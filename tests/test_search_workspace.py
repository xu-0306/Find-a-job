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
        self.remote_type = None
        self.search_criteria = {"keyword": "資料工程 Data"}
        owner = self

        class Source:
            async def close(self):
                pass

            async def search(self, arguments, *, continuation=None, exclude_ids=None, exclude_companies=None, deadline=None, finish_reserve=0):
                owner.search_calls.append((continuation, exclude_ids))
                owner.company_exclusions.append(exclude_companies)
                identifiers = ['alpha', 'beta'] if continuation is None else ['beta', 'gamma']
                if arguments.criteria.keyword == '另一條件':
                    identifiers = ['gamma', 'delta']
                jobs = [Job(source='104', source_job_id=key, title='資料工程 ' + key,
                    company_name='來源公司 ' + key, url='https://www.104.com.tw/job/' + key, remote_type=owner.remote_type)
                    for key in identifiers if '104:' + key not in (exclude_ids or set())]
                return {'items': jobs, 'source_results': [{'source': '104', 'status': 'success', 'returned_count': len(jobs)}],
                    'area_results': [{'area_code': 'TPE', 'area_name': '台北市', 'source': '104',
                        'status': 'success', 'returned_count': len(jobs), 'total_declared': 2,
                        'message': '合成來源訊息', 'completed': True}],
                    'filter_warnings': [], 'continuation': {'next_page': 2}, 'can_continue': continuation is None}

            async def detail(self, job, *, deadline=None, finish_reserve=0):
                return {'status': 'success', 'identity_verified': True,
                    'detail': {'summary': '已核對身份的職缺全文', 'title': job.title}, 'limitations': []}

        self.app.state.sources = Source()
        self.provider_calls = []

        def provider(request):
            payload = json.loads(request.content)
            self.provider_calls.append(payload)
            if len(self.provider_calls) == 1:
                return response(protocol, call=('search_1', 'search_jobs', encode({
                    'criteria': owner.search_criteria, 'sources': ['104'], 'limit': 2})))
            return response(protocol, text='已取得候選，請查看清單。')

        self.app.state.llm_transport = httpx.MockTransport(provider)

    def first(self):
        output = events(self.client.post('/api/chat', json={'request_id': 'first', 'text': '合成搜尋'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        return next(data for event, data in output if event == 'jobs'), output

    def test_include_companies_snapshot_and_continuation_keep_filter(self):
        from app.sources import Sources
        self.configure()
        pages = []

        async def provider(name, args):
            number = args['page']; pages.append(number)
            return dict(jobs=[dict(job_no=f'{number}{i}', job_name='資料工程師', company=company, is_ad=False,
                url=f'https://www.104.com.tw/job/u{number}{i}') for i, company in enumerate(
                ['雲岑曜石股份有限公司', '其他僱主'])], total=4, has_next=number < 2), None

        self.app.state.sources = Sources(mcp_call=provider)
        self.script(('search_jobs', {'criteria': {'keyword': '雲岑曜石'}, 'sources': ['104'], 'limit': 1,
                                    'include_companies': ['雲岑曜石']}), '已整理。')
        output = self.chat('include-first', text='只查104')
        first = next(data for event, data in output if event == 'jobs')
        self.assertEqual(first['criteria_input']['include_companies'], ['雲岑曜石'])
        self.assertEqual([item['source_job_id'] for item in first['items']], ['10'])
        self.assertEqual(first['excluded_count'], 1)
        warnings = first['filter_warnings']
        self.assertEqual(warnings.count('來源回傳2筆，公司名稱符合1筆；其餘是標題或內文提到該名稱的其他公司職缺，已略過。'), 1)
        continued = self.chat('include-next', action={'type': 'continue_search', 'search_id': first['search_id']})
        self.assertEqual(continued[-1][1]['status'], 'completed', continued)
        second = next(data for event, data in continued if event == 'jobs')
        self.assertEqual(second['criteria_input']['include_companies'], ['雲岑曜石'])
        self.assertEqual([item['source_job_id'] for item in second['items']], ['20'])
        self.assertEqual(second['excluded_count'], 1)
        self.assertEqual(second['filter_warnings'], warnings)
        self.assertEqual(pages, [1, 2])
        self.assertFalse(second['can_continue'])

    def test_remote_type_is_saved_returned_and_replayed(self):
        self.configure()
        self.remote_type = "fully_remote"
        self.search_criteria = {"keyword": "實習", "remote_only": True}
        first, _ = self.first()
        job = first["items"][0]
        self.assertEqual(first["area_results"][0]["area_name"], "台北市")
        outputs = [item for item in self.provider_calls[-1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in outputs], ["search_1"])
        tool_result = json.loads(outputs[0]["output"])
        self.assertEqual(tool_result["area_results"], first["area_results"])
        self.assertEqual(tool_result["area_results"][0]["message"], "合成來源訊息")
        self.assertEqual(job["remote_type"], "fully_remote")
        with self.store.connect() as db:
            stored = json.loads(db.execute("SELECT payload_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
        self.assertEqual(stored["remote_type"], "fully_remote")
        history = self.client.get("/api/messages", params={"conversation_id": "legacy"}).json()
        replayed_search = next(search for search in history["conversation_state"]["searches"] if search["items"])
        replayed = replayed_search["items"][0]
        self.assertEqual(replayed["remote_type"], "fully_remote")
        self.assertEqual(replayed_search["area_results"][0]["area_name"], "台北市")
        recommendation_requests = self.script(("present_job_recommendations", {"items": [{"job_id": job["id"]}]}), "推薦一筆。")
        output = self.chat("remote-recommendation", text="推薦")
        recommendation = next(data for event, data in output if event == "recommendations")
        self.assertEqual(recommendation["items"][0]["job"]["remote_type"], "fully_remote")
        references = [json.loads(item["content"].split("\n", 1)[1])
            for item in json.loads(recommendation_requests[-1])["input"] if item.get("role") == "user"
            and isinstance(item.get("content"), str) and item["content"].startswith("以下是這段對話已保存的參考資料")]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0][0]["area_results"], [{key: first["area_results"][0][key]
            for key in ("area_name", "source", "status", "completed")}])

    def test_legacy_saved_job_reads_and_history_default_remote_type_to_null(self):
        self.configure()
        first, _ = self.first()
        job_id = first["items"][0]["id"]
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            payload = json.loads(row[0]); payload.pop("remote_type", None)
            db.execute("UPDATE jobs SET payload_json=? WHERE id=?", (encode(payload), job_id))
            row = db.execute("SELECT content_json FROM messages WHERE request_id='first' AND role='assistant'").fetchone()
            content = json.loads(row[0])
            for event in content["events"]:
                if event["event"] == "jobs":
                    event["data"].pop("area_results", None)
                    for item in event["data"]["items"]: item.pop("remote_type", None)
            db.execute("UPDATE messages SET content_json=? WHERE request_id='first' AND role='assistant'",
                       (encode(content),))
        self.client.put("/api/favorites/" + job_id)
        favorite = self.client.get("/api/favorites").json()["items"][0]
        self.assertIsNone(favorite.get("remote_type"))
        history = self.client.get("/api/messages", params={"conversation_id": "legacy"}).json()
        replayed_search = next(search for search in history["conversation_state"]["searches"] if search["items"])
        replayed = replayed_search["items"][0]
        self.assertIsNone(replayed.get("remote_type"))
        self.assertNotIn("area_results", replayed_search, "legacy job events without area diagnostics still replay")

    def test_remote_search_started_summary_names_remote_filter(self):
        self.configure()
        self.search_criteria = {"keyword": "工程師", "remote_only": True}
        _, output = self.first()
        summaries = [data["summary"] for event, data in output if event == "tool_status"
                     and data.get("name") == "search_jobs" and data.get("phase") == "started"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("搜尋 工程師 · 僅遠端 · ", summaries[0])

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

    def test_duplicate_recommendations_report_accepted_count_and_progress(self):
        self.configure()
        first, _ = self.first()
        job_id = first["items"][0]["id"]
        self.script(("present_job_recommendations", {"items": [{"job_id": job_id}] * 3}), "推薦一筆。")
        output = self.chat("duplicate-recommendations", text="推薦")
        result = next(data for event, data in output if event == "recommendations")
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["accepted_count"], len(result["items"]))
        progress = [data for event, data in output if event == "tool_status" and data.get("name") == "present_job_recommendations"]
        self.assertEqual([item["summary"] for item in progress if item["phase"] == "started"], ["更新精選"])
        self.assertEqual([item["summary"] for item in progress if item["phase"] == "completed"], ["整理 1 筆精選"])
        self.assertEqual(self.state()["shortlist"], [job_id])
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
        self.assertEqual(next(data['message'] for event, data in output if event == 'error'), '這段對話排除的公司已超過 50 家；請在職缺清單面板移除部分已排除公司。')
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
        self.assertFalse(next(data for name, data in output if name == 'recommendations')['replace'])
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
        expected = {'excluded_companies': [], 'shortlist': [beta], 'removed': [{'job_id': alpha, 'by': 'agent', 'reason': '外派'}]}
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
                ('unknown', {'items': [{'job_id': 'job-not-here'}]}, 'unknown_job'),
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


    def test_restore_company_replays_and_can_exclude_again(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = [item['id'] for item in first['items']]
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}, {'job_id': beta}]}))
        self.chat('featured')
        self.script(('search_jobs', {'criteria': {'keyword': '另一條件'}, 'exclude_companies': ['來源公司 alpha']}))
        self.chat('exclude')
        url = '/api/conversations/legacy/excluded-companies'
        self.assertEqual(self.client.patch(url, json={'company': '來源公司 alpha'}).status_code, 200)
        current = self.client.get('/api/messages', params={'conversation_id': 'legacy'}).json()['conversation_state']
        self.assertEqual(current['excluded_companies'], [])
        self.assertEqual(current['removed'], [])
        self.assertEqual(current['shortlist'], [alpha, beta])
        requests = self.script(('search_jobs', {'criteria': {'keyword': '新條件'}}))
        self.chat('after-restore')
        self.assertEqual(self.company_exclusions[-1], [])
        working = next(json.loads(item['content'].split('\n', 1)[1]) for item in json.loads(requests[0])['input']
                       if isinstance(item.get('content'), str) and '"shortlist"' in item['content'])
        self.assertEqual(working['excluded_companies'], [])
        self.assertEqual(working['removed'], [])
        self.script('續查。')
        self.chat('after-restore-continue', action={'type': 'continue_search', 'search_id': first['search_id']})
        self.assertEqual(self.company_exclusions[-1], [])
        self.script(('search_jobs', {'criteria': {'keyword': '再排除'}, 'exclude_companies': ['來源公司 alpha']}))
        self.chat('exclude-again')
        self.assertEqual(self.state()['excluded_companies'], ['來源公司 alpha'])
        self.assertIn(alpha, [item['job_id'] for item in self.state()['removed']])

    def test_restore_company_authorization_and_csrf(self):
        self.configure()
        self.first()
        # Use another owner's conversation, rather than just a missing id.
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO users SELECT 'other','other',NULL,0,1,created_at FROM users WHERE id='default'")
            db.execute("INSERT INTO conversations SELECT 'other-chat',title,created_at,updated_at,'other' FROM conversations WHERE id='legacy'")
        body = {'company': '合成公司'}
        self.assertEqual(self.client.patch('/api/conversations/other-chat/excluded-companies', json=body).status_code, 404)
        self.assertEqual(self.client.patch('/api/conversations/legacy/excluded-companies', json=body,
                                          headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.patch('/api/conversations/legacy/excluded-companies', json=body).status_code, 401)

    def test_restore_metadata_survives_edit_and_stream_snapshots(self):
        self.configure()
        first, _ = self.first()
        alpha = first['items'][0]['id']
        self.script(('search_jobs', {'criteria': {'keyword': '另一條件'}, 'exclude_companies': ['來源公司 alpha']}))
        self.chat('exclude')
        self.client.patch('/api/conversations/legacy/excluded-companies', json={'company': '來源公司 alpha'})
        with self.store.connect() as db:
            row = db.execute("SELECT id,rowid FROM messages WHERE request_id='exclude' AND role='user'").fetchone()
            assistant_seq = db.execute("SELECT rowid FROM messages WHERE request_id='exclude' AND role='assistant'").fetchone()[0]
        self.script('重新回答。')
        self.chat('edited-exclude', edit_message_id=row['id'], edit_request_id='exclude')
        with self.store.connect() as db:
            user = db.execute("SELECT rowid,content_json FROM messages WHERE request_id='edited-exclude' AND role='user'").fetchone()
            self.assertEqual(user[0], row['rowid'])
            self.assertEqual(json.loads(user[1])['company_restorations'], [{'company': '來源公司 alpha', 'after_event': 0}])
            self.assertEqual(db.execute("SELECT rowid FROM messages WHERE request_id='edited-exclude' AND role='assistant'").fetchone()[0], assistant_seq)
        self.assertEqual(self.state()['excluded_companies'], [])
        self.assertNotIn(alpha, [item['job_id'] for item in self.state()['removed']])
        # A new exclusion in the regenerated reply follows the preserved cancellation.
        self.script(('search_jobs', {'criteria': {'keyword': '重做'}, 'exclude_companies': ['來源公司 alpha']}))
        self.chat('edited-again', edit_message_id=row['id'], edit_request_id='edited-exclude')
        self.assertEqual(self.state()['excluded_companies'], ['來源公司 alpha'])

    def test_empty_items_with_removals_keep_other_featured_jobs(self):
        from app.chat import SYSTEM
        self.assertNotIn('比較既有職缺也使用此工具', SYSTEM)
        self.configure()
        first, _ = self.first()
        alpha, beta = [item['id'] for item in first['items']]
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}, {'job_id': beta}]}))
        self.chat('two-featured')
        self.script(('present_job_recommendations', {'items': [], 'removed': [{'job_id': alpha, 'reason': '外派'}]}))
        output = self.chat('remove-one')
        self.assertFalse(next(data['replace'] for event, data in output if event == 'recommendations'))
        self.assertEqual(self.state()['shortlist'], [beta])
        started = [data['summary'] for event, data in output if event == 'tool_status'
                   and data['name'] == 'present_job_recommendations' and data['phase'] == 'started']
        self.assertEqual(started, ['移出 1 筆'])
        completed = [data['summary'] for event, data in output if event == 'tool_status'
                     and data['name'] == 'present_job_recommendations' and data['phase'] == 'completed']
        self.assertEqual(completed, ['移出 1 筆'])
        self.script(('present_job_recommendations', {'items': []}))
        output = self.chat('clear-featured')
        self.assertEqual(self.state()['shortlist'], [])
        completed = [data['summary'] for event, data in output if event == 'tool_status'
                     and data['name'] == 'present_job_recommendations' and data['phase'] == 'completed']
        self.assertEqual(completed, ['清空精選'])

    def test_unknown_removed_is_ignored_without_clearing_shortlist(self):
        self.configure()
        first, _ = self.first()
        alpha, beta = [item['id'] for item in first['items']]
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}, {'job_id': beta}]}))
        self.chat('select-two')
        self.script(('present_job_recommendations', {'removed': [{'job_id': 'unknown-id', 'reason': '不存在'}]}))
        output = self.chat('unknown-only')
        self.assertEqual(self.state()['shortlist'], [alpha, beta])
        result = next(data for event, data in output if event == 'recommendations')
        self.assertEqual(result, {'items': [], 'removed': [], 'replace': False, 'accepted_count': 0})
        self.script(('present_job_recommendations', {'items': [], 'removed': [{'job_id': 'unknown-id', 'reason': '不存在'}]}))
        output = self.chat('unknown-with-empty-items')
        self.assertEqual(self.state()['shortlist'], [alpha, beta])
        self.assertEqual(next(data for event, data in output if event == 'recommendations'),
                         {'items': [], 'removed': [], 'replace': False, 'accepted_count': 0})
        self.script(('present_job_recommendations', {'items': [], 'removed': [
            {'job_id': 'unknown-id', 'reason': '不存在'}, {'job_id': alpha, 'reason': '地點不符'}]}))
        self.chat('mixed-removed')
        self.assertEqual(self.state()['shortlist'], [beta])
        self.assertEqual(self.state()['removed'], [{'job_id': alpha, 'by': 'agent', 'reason': '地點不符'}])

    def test_edit_bounds_working_list_sources_exclusions_and_allowed_jobs(self):
        self.configure()
        first, _ = self.first()
        alpha = first['items'][0]['id']
        self.store.save_reply('first', {'text': '第一輪', 'status': 'completed', 'events': [
            {'event': 'jobs', 'data': first}, {'event': 'recommendations', 'data': {'items': [{'job_id': alpha}], 'replace': True}}]})
        self.script(('present_job_recommendations', {'items': [{'job_id': alpha}]}))
        self.chat('middle', text='再比較')
        with self.store.connect() as db:
            middle = dict(db.execute("SELECT id,rowid FROM messages WHERE request_id='middle' AND role='user'").fetchone())
        self.script(('search_jobs', {'criteria': {'keyword': '另一條件'}, 'exclude_companies': ['未來公司']}),
                    ('present_job_recommendations', {'removed': [{'job_id': alpha, 'reason': '未來決定'}]}))
        later = self.chat('future', text='只查104')
        future_id = next(data['items'][0]['id'] for event, data in later if event == 'jobs')
        self.client.patch('/api/conversations/legacy/excluded-companies', json={'company': '未來公司'})
        requests = self.script(('search_jobs', {'criteria': {'keyword': '重找'}, 'sources': ['104']}),
                               ('present_job_recommendations', {'items': [{'job_id': alpha}]}))
        output = self.chat('edit-middle', text='再找', edit_message_id=middle['id'], edit_request_id='middle')
        work = next(json.loads(item['content'].split('\n', 1)[1]) for item in json.loads(requests[0])['input']
                    if isinstance(item.get('content'), str) and '"shortlist"' in item['content'])
        self.assertEqual(work, {'shortlist': [alpha], 'excluded_companies': [], 'removed': [], 'dismissed_ids': []})
        self.assertEqual(self.company_exclusions[-1], [])
        self.assertEqual(next(data['sources'] for event, data in output if event == 'jobs'), ['104', '518'])
        self.assertTrue(any(event == 'recommendations' for event, data in output))
        with self.store.connect() as db:
            boundary = self.store.turn_before_seq(db, 'edit-middle')
            self.assertNotIn(future_id, self.store.conversation_job_ids(db, 'legacy', boundary))
            self.assertEqual(self.store.conversation_user_texts(db, 'legacy', boundary), ['合成搜尋', '再找'])

    def test_same_turn_search_can_recommend_new_candidate(self):
        self.configure()
        count = 0
        def provider(request):
            nonlocal count
            count += 1
            payload = json.loads(request.content)
            if count == 1:
                return response(call=('search', 'search_jobs', encode({'criteria': {'keyword': '資料'}})))
            if count == 2:
                result = next(json.loads(item['output']) for item in payload['input'] if item.get('type') == 'function_call_output')
                return response(call=('present', 'present_job_recommendations', encode({'items': [{'job_id': result['items'][0]['id']}]})))
            return response(text='完成')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = self.chat('same-turn')
        job_id = next(data['items'][0]['id'] for event, data in output if event == 'jobs')
        self.assertEqual(self.state()['shortlist'], [job_id])


    def test_edited_turn_sees_its_new_jobs_and_exclusions(self):
        self.configure()
        self.first()
        self.script('原回覆')
        self.chat('middle-to-edit')
        with self.store.connect() as db:
            middle = db.execute("SELECT id FROM messages WHERE request_id='middle-to-edit' AND role='user'").fetchone()[0]
        self.script('後面的回覆')
        self.chat('future-turn', text='只查104')
        count, new_ids = 0, []

        def provider(request):
            nonlocal count
            count += 1
            payload = json.loads(request.content)
            if count == 1:
                return response(call=('new-search', 'search_jobs', encode({'criteria': {'keyword': '另一條件'}})))
            if count == 2:
                result = next(json.loads(item['output']) for item in payload['input'] if item.get('type') == 'function_call_output')
                new_ids.extend(item['id'] for item in result['items'])
                return response(call=('exclude-new', 'search_jobs', encode({
                    'criteria': {'keyword': '資料'}, 'exclude_companies': ['來源公司 gamma']})))
            if count == 3:
                result = [json.loads(item['output']) for item in payload['input'] if item.get('type') == 'function_call_output'][-1]
                self.assertEqual(result['excluded_companies'], ['來源公司 gamma'])
                self.assertEqual(result['excluded_job_ids'], [new_ids[0]])
                return response(call=('present-new', 'present_job_recommendations', encode({'items': [{'job_id': new_ids[1]}]})))
            return response(text='重生完成')

        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = self.chat('edited-same-turn', edit_message_id=middle, edit_request_id='middle-to-edit')
        self.assertEqual(count, 4)
        self.assertEqual(self.company_exclusions[-2:], [[], ['來源公司 gamma']])
        self.assertEqual(next(data['sources'] for event, data in output if event == 'jobs'), ['104', '518'])
        with self.store.connect() as db:
            bounded = self.store.conversation_state(db, 'legacy', self.store.turn_before_seq(db, 'edited-same-turn'))
        self.assertEqual(bounded['shortlist'], [new_ids[1]])
        self.assertEqual(bounded['removed'], [{'job_id': new_ids[0], 'by': 'company', 'reason': '公司已排除'}])


class ReplayUnits(unittest.TestCase):
    def test_restore_order_and_agent_removals_with_in_memory_database(self):
        import sqlite3
        from app.store import Store
        with sqlite3.connect(':memory:') as db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE messages(id TEXT,request_id TEXT,role TEXT,content_json TEXT,conversation_id TEXT)')
            saved = [
                {'event': 'jobs', 'data': {'items': [{'id': 'alpha', 'company_name': '甲公司'}, {'id': 'beta', 'company_name': '乙公司'}]}},
                {'event': 'recommendations', 'data': {'items': [{'job_id': 'alpha'}], 'removed': [{'job_id': 'beta', 'reason': '外派'}]}},
                {'event': 'jobs', 'data': {'excluded_companies': ['甲公司'], 'excluded_job_ids': ['alpha']}}]
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', ('u', 'one', 'user', encode({'text': '不要1111'}), 'test'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', ('a', 'one', 'assistant', encode({'events': saved}), 'test'))
            Store.restore_company(db, 'test', '甲公司')
            state = Store.conversation_state(db, 'test')
            self.assertEqual(state['excluded_companies'], [])
            self.assertEqual(state['shortlist'], ['alpha'])
            self.assertEqual(state['removed'], [{'job_id': 'beta', 'by': 'agent', 'reason': '外派'}])
            # A later streamed exclusion must follow the user metadata, rather than overwrite it.
            saved.append({'event': 'jobs', 'data': {'excluded_companies': ['甲公司'], 'excluded_job_ids': ['alpha']}})
            db.execute("UPDATE messages SET content_json=? WHERE id='a'", (encode({'events': saved}),))
            self.assertEqual(Store.conversation_state(db, 'test')['excluded_companies'], ['甲公司'])
            self.assertEqual(Store.conversation_state(db, 'test')['shortlist'], [])
            Store.restore_company(db, 'test', '甲公司')
            self.assertEqual(Store.conversation_state(db, 'test')['shortlist'], ['alpha'])
            boundary = Store.turn_before_seq(db, 'one')
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', ('u2', 'two', 'user', encode({'text': '只查104', 'search_dismissed': {'later': ['alpha']}}), 'test'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', ('a2', 'two', 'assistant', encode({'events': [
                {'event': 'jobs', 'data': {'items': [{'id': 'future', 'company_name': '未來'}], 'excluded_companies': ['未來公司']}}]}), 'test'))
            Store.restore_company(db, 'test', '未來公司')
            limited = Store.conversation_state(db, 'test', boundary)
            self.assertEqual(limited['excluded_companies'], [])
            self.assertEqual(limited['dismissed_ids'], [])
            self.assertEqual(Store.conversation_job_ids(db, 'test', boundary), ['alpha', 'beta'])
            self.assertEqual(Store.conversation_user_texts(db, 'test', boundary), ['不要1111'])
            self.assertEqual(Store.conversation_state(db, 'test')['dismissed_ids'], ['alpha'])


del ChatChecks

if __name__ == '__main__':
    unittest.main()
