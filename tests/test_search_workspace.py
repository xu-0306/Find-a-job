"""Search actions use saved candidates and cursors, never model-generated URLs."""
import json
import unittest

import httpx

from app.store import Job, encode
from test_m2_chat import ChatChecks, events, response


class WorkspaceChecks(unittest.TestCase):
    setUp = ChatChecks.setUp
    tearDown = ChatChecks.tearDown

    def configure(self, protocol='responses'):
        from test_m2_chat import SETTINGS
        with self.store.connect(write=True) as db:
            self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
        self.search_calls = []
        self.company_exclusions = []
        owner = self

        class Source:
            async def search(self, arguments, *, continuation=None, exclude_ids=None, exclude_companies=None):
                owner.search_calls.append((continuation, exclude_ids))
                owner.company_exclusions.append(exclude_companies)
                identifiers = ['alpha', 'beta'] if continuation is None else ['beta', 'gamma']
                if arguments.criteria.keyword == '另一條件':
                    identifiers = ['gamma', 'delta']
                jobs = [Job(source='104', source_job_id=key, title='資料工程 ' + key,
                    company_name='來源公司 ' + key, url='https://www.104.com.tw/job/' + key)
                    for key in identifiers if '104:' + key not in (exclude_ids or set())]
                return {'items': jobs, 'source_results': [{'source': '104', 'status': 'success', 'returned_count': len(jobs)}],
                    'filter_warnings': [], 'continuation': {'next_page': 2}, 'can_continue': continuation is None}

            async def detail(self, job):
                return {'status': 'success', 'identity_verified': True,
                    'detail': {'summary': '已核對身份的職缺全文', 'title': job.title}, 'limitations': []}

        self.app.state.sources = Source()
        self.provider_calls = []

        def provider(request):
            payload = json.loads(request.content)
            self.provider_calls.append(payload)
            if len(self.provider_calls) == 1:
                return response(protocol, call=('search_1', 'search_jobs', encode({
                    'criteria': {'keyword': '資料工程 Data'}, 'sources': ['104'], 'limit': 2})))
            return response(protocol, text='已取得候選，請查看清單。')

        self.app.state.llm_transport = httpx.MockTransport(provider)

    def first(self):
        output = events(self.client.post('/api/chat', json={'request_id': 'first', 'text': '合成搜尋'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        return next(data for event, data in output if event == 'jobs'), output

    def test_continue_dismiss_undo_history_and_private_cursor(self):
        self.configure()
        first, output = self.first()
        self.assertNotIn('_continuation', first)
        self.assertEqual(first['new_count'], 2)
        self.assertEqual([item['citation_number'] for item in first['items']], [1, 2])
        search_id, job_id = first['search_id'], first['items'][1]['id']
        change = {'conversation_id': 'legacy', 'job_id': job_id, 'dismissed': True}
        self.assertEqual(self.client.patch('/api/searches/' + search_id, json=change).json()['dismissed_ids'], [job_id])
        history = self.client.get('/api/messages').json()['items']
        stored = next(event['data'] for message in history for event in message['content']['events'] if event['event'] == 'jobs')
        self.assertEqual(stored['dismissed_ids'], [job_id])
        self.assertNotIn('_continuation', stored)
        continued = events(self.client.post('/api/chat', json={'request_id': 'next', 'text': '繼續找',
            'action': {'type': 'continue_search', 'search_id': search_id}}))
        self.assertEqual(continued[-1][1]['status'], 'completed', continued)
        second = next(data for event, data in continued if event == 'jobs')
        self.assertEqual(second['new_count'], 1)
        self.assertEqual(second['total_count'], 3)
        self.assertEqual([item['citation_number'] for item in second['items']], [3])
        self.assertEqual(self.search_calls[1], ({'next_page': 2}, {'104:beta'}))
        self.assertEqual(len(self.search_calls), 2)
        self.assertEqual(self.client.patch('/api/searches/' + search_id, json=change | {'dismissed': False}).json()['dismissed_ids'], [])
        self.assertEqual(self.client.post('/api/chat', json={'request_id': 'exhausted', 'text': '繼續',
            'action': {'type': 'continue_search', 'search_id': search_id}}).status_code, 409)
        phases = [(data['call_id'], data['phase']) for event, data in output if event == 'tool_status' and data.get('call_id')]
        self.assertEqual(phases, [('search_1', 'started'), ('search_1', 'completed')])
        self.app.state.llm_transport = httpx.MockTransport(lambda request: response(call=('again', 'search_jobs', encode({
            'criteria': {'keyword': '資料工程 Data'}, 'sources': ['104'], 'limit': 2, 'search_id': search_id}))))
        exhausted = events(self.client.post('/api/chat', json={'request_id': 'model-exhausted', 'text': '再找一次'}))
        self.assertEqual(exhausted[-1][1]['status'], 'interrupted')
        self.assertEqual(next(data['code'] for event, data in exhausted if event == 'error'), 'search_exhausted')
        self.assertEqual(len(self.search_calls), 2)
        self.assertEqual([data['phase'] for event, data in exhausted if event == 'tool_status' and data.get('call_id')], ['started', 'interrupted'])

    def test_search_history_refresh_batches_jobs_and_favorites(self):
        from contextlib import nullcontext
        import sqlite3
        from unittest.mock import patch

        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        # More ids than SQLite's legacy parameter limit, including a job no longer in the jobs table.
        missing = [{'id': f'missing-{i}', 'title': 'saved snapshot'} for i in range(1000)]
        saved = [first | {'items': [first['items'][0], *missing[:500]], '_continuation': {'private': True}},
                 first | {'items': [first['items'][1], *missing[500:]], 'can_continue': False}]
        self.store.save_reply('first', {'text': '', 'status': 'completed',
                                      'events': [{'event': 'jobs', 'data': event} for event in saved]})
        with self.store.connect(write=True) as db:
            current = json.loads(db.execute('SELECT payload_json FROM jobs WHERE id=?', (alpha,)).fetchone()[0])
            current['summary'] = 'current detail'
            db.execute('UPDATE jobs SET payload_json=? WHERE id=?', (encode(current), alpha))
            db.execute("INSERT INTO favorites VALUES(?, 'default', '2026-10-18')", (alpha,))
            db.execute("UPDATE messages SET content_json=json_set(content_json,'$.search_dismissed',json(?)) WHERE request_id='first' AND role='user'",
                       (encode({first['search_id']: [beta]}),))
        expected = json.loads(encode(saved))
        for event in expected:
            event.pop('_continuation', None)
            event['dismissed_ids'] = [beta]
            for item in event['items']:
                if item['id'] == alpha:
                    item.update(current)
                item['is_favorite'] = item['id'] == alpha
        with self.store.connect() as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            queries = []
            db.set_trace_callback(queries.append)
            searches = self.store.conversation_searches(db, 'legacy', 'default')
            self.assertEqual(searches, expected)
            lookups = lambda: [sql for sql in queries if sql.startswith('SELECT') and ('FROM jobs ' in sql or 'FROM favorites ' in sql)]
            self.assertLessEqual(len(lookups()), 4, 'job and favorite reads scale with chunks, not candidates')
            queries.clear()
            with patch.object(self.store, 'connect', return_value=nullcontext(db)):
                history = self.store.messages(conversation_id='legacy', user_id='default')
            self.assertEqual([event['data'] for message in history['items'] for event in message['content']['events']], expected)
            self.assertLessEqual(len(lookups()), 4, 'message history shares the batch refresh path')
            self.assertTrue(all(not item['is_favorite'] for event in self.store.conversation_searches(db, 'legacy', None)
                                for item in event['items']), 'an unspecified user does not inherit another user favorites')

    def test_cross_conversation_cannot_continue_or_dismiss(self):
        self.configure()
        first, _ = self.first()
        other = self.client.post('/api/conversations', json={'title': '另一對話'}).json()['id']
        self.assertEqual(self.client.post('/api/chat', json={'request_id': 'wrong', 'text': '繼續', 'conversation_id': other,
            'action': {'type': 'continue_search', 'search_id': first['search_id']}}).status_code, 404)
        self.assertEqual(self.client.patch('/api/searches/' + first['search_id'], json={
            'conversation_id': other, 'job_id': first['items'][0]['id'], 'dismissed': True}).status_code, 404)
        self.assertEqual(len(self.search_calls), 1)

    def test_citation_numbers_are_stable_across_search_batches(self):
        self.configure()
        self.first()
        calls = 0

        def provider(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(call=('second-search', 'search_jobs', encode({
                    'criteria': {'keyword': '另一條件'}, 'sources': ['104'], 'limit': 2})))
            return response(text='第二批已加入清單。')

        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = events(self.client.post('/api/chat', json={'request_id': 'other-search', 'text': '另一條件'}))
        jobs = next(data['items'] for event, data in output if event == 'jobs')
        self.assertEqual([item['citation_number'] for item in jobs], [3, 4])
        history = self.client.get('/api/messages').json()['items']
        numbers = [job['citation_number'] for message in history for event in message['content']['events']
                   if event['event'] == 'jobs' for job in event['data']['items']]
        self.assertEqual(numbers, [1, 2, 3, 4])

    def test_recommendations_on_followup_three_protocols(self):
        for protocol in ('responses', 'chat_completions', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                self.configure(protocol)
                with self.store.connect(write=True) as db:
                    db.execute('DELETE FROM messages')
                first, _ = self.first()
                job = first['items'][0]
                count = 0

                def provider(request):
                    nonlocal count
                    count += 1
                    if count == 1:
                        self.assertIn(job['id'], request.content.decode())
                        return response(protocol, call=('rec', 'present_job_recommendations', encode({
                            'items': [{'job_id': job['id'], 'reasons': ['工作內容與本次條件相符'], 'unknowns': ['薪資待確認']}]})))
                    return response(protocol, text='請比較卡片上的條件。')

                self.app.state.llm_transport = httpx.MockTransport(provider)
                output = events(self.client.post('/api/chat', json={'request_id': 'compare', 'text': '比較剛才的職缺'}))
                self.assertEqual(output[-1][1]['status'], 'completed', output)
                rec = next(data for event, data in output if event == 'recommendations')['items'][0]
                self.assertEqual(rec['job']['id'], job['id'])
                self.assertEqual(rec['job']['citation_number'], 1)
                self.assertEqual(len(self.search_calls), 1)
                self.client.put('/api/favorites/' + job['id'])
                history = self.client.get('/api/messages').json()['items']
                saved = next(e['data'] for m in history for e in m['content']['events'] if e['event'] == 'recommendations')
                self.assertTrue(saved['items'][0]['job']['is_favorite'])

    def script(self, *steps):
        """Model replies one step per provider call: a (name, arguments) tool call or final text."""
        requests, queue = [], list(steps)

        def provider(request):
            requests.append(request.content.decode())
            step = queue.pop(0) if queue else '完成。'
            if isinstance(step, tuple):
                return response(call=(f'call-{len(requests)}', step[0], encode(step[1])))
            return response(text=step)

        self.app.state.llm_transport = httpx.MockTransport(provider)
        return requests

    def chat(self, request_id, text='繼續', **extra):
        output = events(self.client.post('/api/chat', json={'request_id': request_id, 'text': text, **extra}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        return output

    def state(self):
        with self.store.connect() as db:
            return self.store.conversation_state(db, 'legacy')

    def test_company_exclusion_is_conversation_wide_and_removes_earlier_candidates(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        self.script(('search_jobs', {'criteria': {'keyword': '另一條件'}, 'exclude_companies': ['來源公司 ALPHA']}), '已排除。')
        output = self.chat('exclude')
        jobs = next(data for event, data in output if event == 'jobs')
        self.assertEqual(self.company_exclusions[-1], ['來源公司 ALPHA'])
        self.assertEqual(jobs['excluded_companies'], ['來源公司 ALPHA'])
        self.assertEqual(jobs['excluded_job_ids'], [alpha])
        self.assertEqual(self.state()['removed'], [{'job_id': alpha, 'by': 'company', 'reason': '公司已排除'}])
        self.assertEqual(self.state()['excluded_companies'], ['來源公司 ALPHA'])
        # A later search and a continuation of the first search keep the exclusion without the model repeating it.
        requests = self.script(('search_jobs', {'criteria': {'keyword': '第三條件'}}), '好。')
        self.chat('later')
        self.assertEqual(self.company_exclusions[-1], ['來源公司 ALPHA'])
        self.assertIn('來源公司 ALPHA', requests[0])
        self.script('續查完成。')
        self.chat('continue', action={'type': 'continue_search', 'search_id': first['search_id']})
        self.assertEqual(self.company_exclusions[-1], ['來源公司 ALPHA'])
        self.assertNotIn(beta, [item['job_id'] for item in self.state()['removed']])

    def test_conversation_exclusions_have_a_total_limit(self):
        self.configure()
        self.first()
        for batch in range(2):
            self.script(('search_jobs', {'criteria': {'keyword': f'條件{batch}'}, 'exclude_companies': [f'公司{batch}-{i}' for i in range(20)]}), '好。')
            self.chat(f'exclude-{batch}')
        self.assertEqual(len(self.state()['excluded_companies']), 40)
        # Spelling variants of one company keep the first name and do not count again.
        self.script(('search_jobs', {'criteria': {'keyword': '變體'}, 'exclude_companies': ['公司0-0', '公司0 0', '公司0／0', 'Meta', 'Meta-', 'META--']}), '好。')
        self.chat('exclude-variants')
        self.assertEqual(len(self.state()['excluded_companies']), 41)
        self.assertEqual(self.state()['excluded_companies'][-1], 'Meta')
        self.script(('search_jobs', {'criteria': {'keyword': '條件2'}, 'exclude_companies': [f'公司2-{i}' for i in range(10)]}))
        output = events(self.client.post('/api/chat', json={'request_id': 'exclude-2', 'text': '再排除'}))
        self.assertEqual(next(data['code'] for event, data in output if event == 'error'), 'too_many_exclusions')
        self.assertEqual(len(self.state()['excluded_companies']), 41)

    def test_recommendations_replace_shortlist_and_remove_jobs(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}, {'job_id': beta}]}), '兩筆精選。')
        self.chat('rec-1')
        self.assertEqual(self.state()['shortlist'], [alpha, beta])
        requests = self.script(('present_job_recommendations', {'items': [{'job_id': beta}],
                                                                'removed': [{'job_id': alpha, 'reason': '使用者不要外派'}]}), '已更新。')
        output = self.chat('rec-2')
        event = next(data for name, data in output if name == 'recommendations')
        self.assertEqual(event['removed'], [{'job_id': alpha, 'reason': '使用者不要外派'}])
        self.assertEqual(self.state()['shortlist'], [beta])
        self.assertEqual(self.state()['removed'], [{'job_id': alpha, 'by': 'agent', 'reason': '使用者不要外派'}])
        # The model sees the earlier working list on its next turn.
        working = next(json.loads(item['content'].split('\n', 1)[1]) for item in json.loads(requests[0])['input']
                       if isinstance(item.get('content'), str) and '"shortlist"' in item['content'])
        self.assertEqual(working['shortlist'], [alpha, beta])
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}]}), '改回。')
        self.chat('rec-3')
        self.assertEqual((self.state()['shortlist'], self.state()['removed']), ([alpha], []))
        # Removing only keeps the shortlist; an explicit empty list clears it.
        self.script(('present_job_recommendations', {'removed': [{'job_id': beta, 'reason': '地點太遠'}]}), '已移出。')
        self.chat('rec-4')
        self.assertEqual(self.state()['shortlist'], [alpha])
        self.script(('present_job_recommendations', {'items': [], 'removed': [{'job_id': alpha, 'reason': '不適合'}]}), '已清空。')
        output = self.chat('rec-5')
        self.assertTrue(next(data for name, data in output if name == 'recommendations')['replace'])
        self.assertEqual(self.state()['shortlist'], [])
        self.script(('present_job_recommendations', {'items': [{'job_id': beta}]}), '再選。')
        self.chat('rec-6')
        self.script(('present_job_recommendations', {'items': []}), '全部清空。')
        self.chat('rec-7')
        self.assertEqual(self.state()['shortlist'], [])

    def test_history_pages_carry_the_whole_conversation_working_list(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}]}), '一筆。')
        self.chat('page-1')
        self.script(('present_job_recommendations', {'items': [{'job_id': beta}], 'removed': [{'job_id': alpha, 'reason': '外派'}]}), '換一筆。')
        self.chat('page-2')
        expected = {'shortlist': [beta], 'removed': [{'job_id': alpha, 'by': 'agent', 'reason': '外派'}]}
        latest = self.client.get('/api/messages', params={'conversation_id': 'legacy', 'limit': 2}).json()
        searches = latest['conversation_state'].pop('searches')
        recommendations = latest['conversation_state'].pop('recommendations')
        self.assertEqual(recommendations, [{'job_id': beta, 'reasons': [], 'concerns': [], 'unknowns': [],
                                          'job': first['items'][1]}])
        self.assertEqual(latest['conversation_state'], expected)
        # The first page also carries the searches, although none of its messages hold the jobs event.
        self.assertFalse(any(e['event'] == 'jobs' for m in latest['items'] for e in m['content']['events']))
        self.assertEqual([search['search_id'] for search in searches], [first['search_id']])
        self.assertEqual([job['id'] for job in searches[0]['items']], [alpha, beta])
        self.assertNotIn('_continuation', searches[0])
        # An older page, which only holds the first recommendation, still reports the current list.
        older = self.client.get('/api/messages', params={'conversation_id': 'legacy', 'limit': 2, 'cursor': latest['next_cursor']}).json()
        self.assertFalse(any(e['event'] == 'recommendations' and e['data']['items'][0]['job_id'] == beta
                             for m in older['items'] for e in m['content']['events']))
        self.assertNotIn('searches', older['conversation_state'])
        self.assertEqual(older['conversation_state'].pop('recommendations'), recommendations)
        self.assertEqual(older['conversation_state'], expected)
        self.assertNotIn('conversation_state', self.client.get('/api/messages').json())

    def test_first_page_keeps_current_recommendations_outside_loaded_messages(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        self.script(('present_job_recommendations', {'items': [
            {'job_id': alpha, 'reasons': ['已被取代']}, {'job_id': beta, 'reasons': ['舊理由']}]}), '舊精選。')
        self.chat('recommend-old')
        self.script(('present_job_recommendations', {'items': [
            {'job_id': beta, 'reasons': ['符合技能'], 'concerns': ['需要輪班'], 'unknowns': ['薪資未知']},
            {'job_id': alpha, 'reasons': ['稍後移出']}]}), '新精選。')
        output = self.chat('recommend-current')
        expected = next(data['items'] for name, data in output if name == 'recommendations')
        requests = self.script('普通回答。')
        self.chat('plain-after-recommendation')
        model_working = next(json.loads(item['content'].split('\n', 1)[1]) for item in json.loads(requests[0])['input']
                             if isinstance(item.get('content'), str) and '"shortlist"' in item['content'])
        self.assertEqual(set(model_working), {'shortlist', 'excluded_companies', 'removed', 'dismissed_ids'})
        self.assertNotIn('recommendations', model_working)
        latest = self.client.get('/api/messages', params={'conversation_id': 'legacy', 'limit': 2}).json()
        self.assertFalse(any(e['event'] == 'recommendations' for m in latest['items'] for e in m['content']['events']))
        self.assertEqual(latest['conversation_state']['recommendations'], expected)
        self.assertEqual([item['job_id'] for item in expected], latest['conversation_state']['shortlist'])
        # Removing only retains beta's original recommendation; it does not borrow older notes.
        self.script(('present_job_recommendations', {'removed': [{'job_id': alpha, 'reason': '不適合'}]}), '已移出。')
        self.chat('remove-alpha')
        current = self.client.get('/api/messages', params={'conversation_id': 'legacy', 'limit': 1}).json()['conversation_state']
        self.assertEqual(current['recommendations'], expected[:1])
        self.assertEqual(current['shortlist'], [beta])
        # Company exclusions also remove recommendations from the effective shortlist.
        self.script(('search_jobs', {'criteria': {'keyword': '下一批'}, 'exclude_companies': ['來源公司 beta']}), '排除公司。')
        self.chat('exclude-beta')
        current = self.client.get('/api/messages', params={'conversation_id': 'legacy'}).json()['conversation_state']
        self.assertEqual(current['recommendations'], [])
        self.assertEqual(current['shortlist'], [])

    def test_legacy_recommendation_items_and_edited_replacement(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = (item['id'] for item in first['items'])
        def saved(*recommendations):
            self.store.save_reply('first', {'text': '', 'status': 'completed', 'events': [
                {'event': 'jobs', 'data': first},
                *[{'event': 'recommendations', 'data': data} for data in recommendations]]})
            return self.client.get('/api/messages', params={'conversation_id': 'legacy'}).json()['conversation_state']
        old = {'items': [{'job_id': alpha, 'reasons': ['過期']}, {'job_id': beta, 'reasons': ['過期']} ]}
        legacy = {'items': [{'job_id': beta}]}
        current = saved(old, legacy)
        self.assertEqual(current['recommendations'], [{'job_id': beta, 'reasons': [], 'concerns': [], 'unknowns': []}])
        current = saved(old, {'items': [], 'replace': True})
        self.assertEqual(current['recommendations'], [])
        # Replacing the saved event during an edit drops its previous reasons.
        current = saved({'items': [{'job_id': alpha, 'unknowns': ['新未知']} ]})
        self.assertEqual(current['recommendations'], [{'job_id': alpha, 'reasons': [], 'concerns': [], 'unknowns': ['新未知']}])
        self.assertEqual(saved()['recommendations'], [])

    def test_recommendations_reject_unknown_or_empty_changes(self):
        self.configure()
        self.first()
        for request_id, arguments, code in (
                ('unknown', {'removed': [{'job_id': 'job-not-here', 'reason': '不適合'}]}, 'unknown_job'),
                ('empty', {}, 'invalid_tool'), ('empty-removed', {'removed': []}, 'invalid_tool')):
            with self.subTest(request_id=request_id):
                self.script(('present_job_recommendations', arguments))
                output = events(self.client.post('/api/chat', json={'request_id': request_id, 'text': '整理'}))
                self.assertEqual(next(data['code'] for event, data in output if event == 'error'), code)
        self.assertEqual(self.state()['shortlist'], [])

    def test_verified_details_update_saved_job_and_history(self):
        self.configure()
        first, _ = self.first()
        job_id = first['items'][0]['id']
        detail = self.client.post('/api/jobs/' + job_id + '/details').json()
        self.assertEqual(detail['item']['summary'], '已核對身份的職缺全文')
        history = self.client.get('/api/messages').json()['items']
        job = next(e['data']['items'][0] for m in history for e in m['content']['events'] if e['event'] == 'jobs')
        self.assertEqual(job['summary'], detail['item']['summary'])


del ChatChecks

if __name__ == '__main__':
    unittest.main()
