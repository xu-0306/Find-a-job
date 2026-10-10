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
        owner = self

        class Source:
            async def search(self, arguments, *, continuation=None, exclude_ids=None):
                owner.search_calls.append((continuation, exclude_ids))
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
