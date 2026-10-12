import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from app import security
from app.sources import Sources
from app.store import encode
from test_m2_chat import ChatChecks, response, events
from test_m2_sources import fake104
from test_m2_source518 import SEARCH_HTML
import httpx


class SourceChatChecks(unittest.TestCase):
    setUp = ChatChecks.setUp
    tearDown = ChatChecks.tearDown

    def use_sources(self, service, html=SEARCH_HTML):
        self.source_urls = []
        def transport(req):
            self.source_urls.append(str(req.url)); return httpx.Response(200, text=html)
        self.app.state.sources = Sources(httpx.MockTransport(transport), service, interval=0)

    def set_518(self, enabled):
        self.assertEqual(self.client.put('/api/admin/settings', json={'source_518_enabled': enabled},
                         headers={'X-CSRF-Token': security.csrf(self.admin_token)}).status_code, 200)

    def configure(self, protocol='responses', service=fake104, html=SEARCH_HTML):
        from test_m2_chat import SETTINGS
        with self.store.connect(write=True) as db: self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
        self.use_sources(service, html)
        requests = []
        def provider(req):
            payload = json.loads(req.content); requests.append(payload)
            if len(requests) == 1:
                return response(protocol, call=('s1', 'search_jobs', encode({'criteria': {'keyword': '採購'}, 'limit': 2})))
            return response(protocol, text='部分來源受限，請核對薪資。')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        return requests

    def send(self, ident='source'):
        return self.client.post('/api/chat', json={'request_id': ident, 'text': '合成來源查詢'})

    def test_three_protocols_source_sse_history_and_stable_favorites(self):
        for protocol in ('responses', 'chat_completions', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                self.configure(protocol)
                output = events(self.send(protocol))
                self.assertEqual(output[-1][1]['status'], 'completed')
                result = next(x for event, x in output if event == 'jobs')
                self.assertEqual(len(result['items']), 2)
                ident = result['items'][0]['id']
                self.assertEqual(self.client.put('/api/favorites/'+ident).status_code, 200)
                history = self.client.get('/api/messages').json()['items']
                self.assertTrue(next(x for x in history[-1]['content']['events'] if x['event']=='jobs')['data']['items'][0]['is_favorite'])
                self.configure(protocol); next_result = next(x for event,x in events(self.send(protocol+'2')) if event=='jobs')
                self.assertEqual(next_result['items'][0]['id'], ident)
                self.client.delete('/api/favorites/'+ident)

    def test_cancel_and_revoke_after_request_never_save_late_jobs(self):
        from app import security
        for revoke in (False, True):
            self.configure(); started = threading.Event()
            async def hanging(name, arguments):
                started.set(); await asyncio.sleep(1000)
            self.app.state.sources.mcp_call = hanging
            with ThreadPoolExecutor() as pool:
                reply = pool.submit(self.send, 'revoke' if revoke else 'cancel')
                self.assertTrue(started.wait(3))
                if revoke:
                    with self.store.connect(write=True) as db: db.execute('DELETE FROM sessions WHERE id_hash=?', (security.digest(self.user_token),))
                    # Logout/revocation routes ordinarily also cancel the task; emulate external expiry.
                    self.client.portal.call(self.app.state.active_chats['default']['task'].cancel)
                else:
                    self.assertEqual(self.client.post('/api/chat/cancel/cancel').status_code, 200)
                self.assertEqual(events(reply.result(3))[-1][1]['status'], 'interrupted')
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
            if revoke: break

    def test_unauthorized_search_and_details_make_no_source_calls(self):
        self.configure()
        self.client.cookies.delete('jc_user')
        self.assertEqual(self.send().status_code, 401)
        self.assertEqual(self.client.post('/api/jobs/unknown/details').status_code, 401)

    def provider_search(self, arguments):
        requests = []
        def provider(req):
            requests.append(json.loads(req.content))
            if len(requests) == 1:
                return response('responses', call=('s1', 'search_jobs', encode(arguments)))
            return response('responses', text='已整理。')
        self.app.state.llm_transport = httpx.MockTransport(provider)

    def test_518_switch_off_skips_518_and_says_so_while_on_is_unchanged(self):
        self.configure()
        on = next(x for event, x in events(self.send('on')) if event == 'jobs')
        self.assertEqual(on['sources'], ['104', '518'])
        self.assertTrue(self.source_urls)
        self.assertFalse(any('已在後台停用' in w for w in on['filter_warnings']))
        self.configure(); self.set_518(False)
        off = next(x for event, x in events(self.send('off')) if event == 'jobs')
        self.assertEqual(off['sources'], ['104'])
        self.assertEqual(self.source_urls, [])
        self.assertIn('518 已在後台停用，本次未查詢。', off['filter_warnings'])
        self.assertEqual({item['source'] for item in off['items']}, {'104'})

    def test_only_518_requested_while_disabled_is_source_disabled_without_request(self):
        self.configure(); self.set_518(False)
        self.app.state.llm_transport = httpx.MockTransport(lambda req: response('responses', call=(
            's1', 'search_jobs', encode({'criteria': {'keyword': '採購'}, 'sources': ['518']}))))
        output = events(self.send('only518'))
        self.assertIn('source_disabled', [x.get('code') for event, x in output if event == 'error'])
        self.assertEqual(self.source_urls, [])
        self.assertFalse(any(event == 'jobs' for event, _ in output))

    def test_continue_while_518_disabled_queries_only_104_new_page(self):
        pages = []
        async def paged(name, args):
            page = args['page']; pages.append(page)
            return {'jobs': [{'job_no': f'p{page}', 'job_name': '採購', 'company': '合成公司',
                              'url': f'https://www.104.com.tw/job/p{page}', 'appear_date': '20261005'}],
                    'total': 2, 'has_next': page < 2}, None
        self.configure(service=paged)
        self.provider_search({'criteria': {'keyword': '採購'}, 'limit': 1})
        first = next(x for event, x in events(self.send('first')) if event == 'jobs')
        self.assertTrue(first['can_continue'])
        self.assertEqual(pages, [1])
        self.set_518(False); self.source_urls.clear()
        self.app.state.llm_transport = httpx.MockTransport(lambda req: response('responses', text='已整理。'))
        output = events(self.client.post('/api/chat', json={'request_id': 'next', 'text': '繼續找',
            'action': {'type': 'continue_search', 'search_id': first['search_id']}}))
        self.assertEqual(output[-1][1]['status'], 'completed')
        second = next(x for event, x in output if event == 'jobs')
        self.assertEqual(self.source_urls, [])
        self.assertEqual(pages, [1, 2])
        self.assertEqual([item['source_job_id'] for item in second['items']], ['p2'])
        self.assertEqual(second['sources'], ['104'])
        self.assertIn('518 已在後台停用，本次未查詢。', second['filter_warnings'])

    def test_continue_when_only_518_has_more_reports_exhausted_without_request(self):
        async def single(name, args):
            return {'jobs': [{'job_no': '12', 'job_name': '採購', 'company': '合成公司',
                              'url': 'https://www.104.com.tw/job/a12', 'appear_date': '20261005'}],
                    'total': 1, 'has_next': False}, None
        # Two 518 cards: with limit 1 the second stays pending, so only 518 has more after the first page.
        card = ('<div class="job__card" data-id="A2" data-url="/job-A2.html"><h2><a class="job__title" href="/job-A2.html">'
                '採購人員</a></h2><a class="job__comp" href="/company-C1.html"><span class="job__comp__name">測試公司</span></a>'
                '<p class="job__salary">月薪 32,000 至 35,000 元</p><ul class="job__summaries"><li>台南市-安定區</li></ul>'
                '<span class="job__date">10/05</span></div>')
        two_cards = SEARCH_HTML.replace('<div id="listContent">', '<div id="listContent">' + card)
        self.configure(service=single, html=two_cards)
        self.provider_search({'criteria': {'keyword': '採購'}, 'limit': 1})
        first = next(x for event, x in events(self.send('first')) if event == 'jobs')
        self.assertTrue(first['can_continue'])
        self.set_518(False); self.source_urls.clear()
        self.app.state.llm_transport = httpx.MockTransport(lambda req: response('responses', text='已整理。'))
        output = events(self.client.post('/api/chat', json={'request_id': 'next', 'text': '繼續找',
            'action': {'type': 'continue_search', 'search_id': first['search_id']}}))
        self.assertIn('search_exhausted', [x.get('code') for event, x in output if event == 'error'])
        self.assertEqual(self.source_urls, [])

    def detail_provider(self, arguments, protocol='responses'):
        requests = []
        def provider(req):
            payload = json.loads(req.content); requests.append(payload)
            if len(requests) == 1:
                tools = {tool.get('name', tool.get('function', {}).get('name')):
                         tool.get('function', tool) for tool in payload['tools']}
                self.assertIn('url', tools['get_job_details'].get('parameters', tools['get_job_details'].get('input_schema'))['properties'])
                self.assertIn('只有使用者要找、推薦或比較職缺', tools['search_jobs']['description'])
                from app.chat import SYSTEM
                for principle in ('詢問特定職缺時不搜尋', '不用搜尋去找', '1～3句', '尚未確認'):
                    self.assertIn(principle, SYSTEM)
                self.assertIn(SYSTEM, json.dumps(payload, ensure_ascii=False))
                return response(protocol, call=('detail1', 'get_job_details', encode(arguments)))
            self.assertIn('identity_verified', json.dumps(payload))
            return response(protocol, text='先依職缺內容分析；尚未確認的資訊請補充。')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        return requests

    def test_url_detail_roundtrip_preserves_identity_cleaning_and_no_candidates(self):
        from test_m2_chat import SETTINGS
        calls = []
        async def detail(name, args):
            calls.append((name, args))
            self.assertEqual(name, 'get_job_detail')
            self.assertEqual(args, {'detail_id': '91mtc'})
            return ({'job_no': '15192528', 'job_name': '合成採購', 'company': '合成公司',
                     'description': '<b>供應鏈工作</b> 請寄 secret@example.invalid'},
                    {'data': {'header': {'analysisUrl': '/jobs/main/analysis/91mtc'}}})
        for protocol in ('responses', 'chat_completions', 'anthropic_messages'):
            for suffix in ('', '?jobsource=google'):
                with self.subTest(protocol=protocol, suffix=suffix):
                    with self.store.connect(write=True) as db: self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
                    self.use_sources(detail)
                    url = 'https://www.104.com.tw/job/91mtc' + suffix
                    requests = self.detail_provider({'url': url}, protocol)
                    output = events(self.client.post('/api/chat', json={'request_id': protocol+str(bool(suffix)),
                        'text': '分析這則職缺與職涯方向 '+url}))
                    self.assertEqual(output[-1][1]['status'], 'completed', output)
                    result = next(data for event, data in output if event == 'job_detail')
                    self.assertTrue(result['identity_verified'])
                    self.assertIsNone(result['item']); self.assertIsNone(result['job_id'])
                    self.assertEqual(result['url'], url)
                    self.assertIn('供應鏈工作', result['detail']['summary'])
                    self.assertNotIn('<b>', result['detail']['summary'])
                    self.assertNotIn('secret@example.invalid', result['detail']['summary'])
                    self.assertFalse(any(event in ('jobs', 'recommendations') for event, _ in output))
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(self.source_urls, [])
                    with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
                    history = self.client.get('/api/messages').json()['items'][-1]['content']['events']
                    self.assertFalse(any(event['event'] == 'jobs' for event in history))
        self.assertEqual(len(calls), 6)

    def test_url_detail_cannot_verify_echoed_url_or_unrelated_analysis_url(self):
        for index, analysis in enumerate((None, '/jobs/main/analysis/other')):
            async def detail(name, args):
                return ({'job_no': '15192528', 'url': 'https://www.104.com.tw/job/91mtc', 'description': '合成內容'},
                        {'data': {'header': {'analysisUrl': analysis}}})
            self.configure(service=detail)
            self.detail_provider({'url': 'https://www.104.com.tw/job/91mtc'})
            output = events(self.send('unverified'+str(index)))
            result = next(data for event, data in output if event == 'job_detail')
            self.assertFalse(result['identity_verified'])
            self.assertIsNone(result['item'])
            self.assertTrue(result['limitations'])

    def test_saved_job_id_tool_and_api_still_update_verified_job(self):
        from app.store import Job
        job_id = self.store.save_job(Job(source='104', source_job_id='15192528', title='既有職稱',
            company_name='合成公司', url='https://www.104.com.tw/job/91mtc'))
        async def detail(name, args):
            return {'job_no': '15192528', 'job_name': '新職稱', 'description': '已核對內容'}, None
        self.configure(service=detail)
        self.detail_provider({'job_id': job_id})
        output = events(self.send('saved-detail'))
        result = next(data for event, data in output if event == 'job_detail')
        self.assertEqual(result['job_id'], job_id)
        self.assertEqual(result['item']['title'], '新職稱')
        self.assertTrue(result['identity_verified'])
        self.assertEqual(self.client.post('/api/jobs/'+job_id+'/details').json()['item']['title'], '新職稱')
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_invalid_url_tool_is_rejected_without_any_source_call(self):
        async def blocked(*args): self.fail('Invalid URL must not reach the provider')
        for index, args in enumerate(({'url': 'https://evil.invalid/job/91mtc'},
                {'url': 'https://www.104.com.tw/job/91mtc/extra'},
                {'job_id': 'saved', 'url': 'https://www.104.com.tw/job/91mtc'}, {})):
            self.configure(service=blocked)
            self.detail_provider(args)
            output = events(self.send('invalid-detail'+str(index)))
            self.assertIn('invalid_tool', [data.get('code') for event, data in output if event == 'error'])
            self.assertFalse(any(event in ('jobs', 'job_detail') for event, _ in output))


del ChatChecks  # Reuse setup without rediscovering the original suite in this module.
if __name__ == '__main__': unittest.main()
