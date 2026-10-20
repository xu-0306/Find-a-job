"""Source diagnostics keep the daily contract while areas report coverage separately."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.chat import produce_reply
from app.scheduler import DailyRunner
from app.sources import DailySearchInput, SearchInput, SourceError, Sources, area_catalog
from test_m2_sources import fake104
from test_m2_source518 import SEARCH_HTML
from test_source1111 import page, row


class DailyAdapterChecks(unittest.IsolatedAsyncioTestCase):
    async def test_optional_area_diagnostics_do_not_abort_legacy_source_batches(self):
        store = MagicMock()
        store.get.return_value = {'source_518_enabled': True}
        preference = dict(id='p', revision=1, user_id='default', criteria={'keyword': 'planner'})
        area = dict(area_code=None, area_name='all', source='518', status='failed',
                    returned_count=0, total_declared=None, message='failed', completed=False)
        provider = SimpleNamespace(search=AsyncMock(side_effect=[
            dict(items=[], source_results=[dict(source='104', status='empty', baseline_eligible=True)]),
            dict(items=[], source_results=[dict(source='518', status='failed', baseline_eligible=False)],
                 area_results=[area]),
        ]))
        runner = DailyRunner(store, provider)
        with patch('app.scheduler.effective_preferences', return_value=[preference]), \
                patch('app.scheduler.daily.commit_results', return_value={'new_count': 0}) as commit:
            await runner.execute('run', datetime(2026, 10, 19, tzinfo=timezone.utc), [preference], None)
        self.assertEqual(provider.search.await_count, 2)
        commit.assert_called_once()
        batches = commit.call_args.args[4]
        self.assertEqual([batch['source'] for batch in batches], ['104', '518'])
        self.assertEqual([batch['eligible'] for batch in batches], [True, False])
        self.assertEqual([batch['area_results'] for batch in batches], [[], [area]])


class SourceContractChecks(unittest.IsolatedAsyncioTestCase):
    async def test_v1_1111_total_survives_queued_rows_without_region_fields(self):
        browser = SimpleNamespace(call=AsyncMock(return_value=page([row('123'), row('456')], size=2, total=2)))
        service = Sources(browser1111=browser)
        arguments = SearchInput(criteria={'keyword': 'planner'}, sources=['1111'], limit=1)
        first = await service.search(arguments)
        cursor = json.loads(first['continuation'])
        state = cursor['sources']['1111']
        area = state.pop('areas')['all']
        cursor['version'] = 1
        state.update({key: area[key] for key in ('next_page', 'next_url', 'has_next', 'remaining',
                                                'pages', 'visited', 'page_signatures', 'total_declared')})
        resumed = await service.search(arguments, continuation=cursor)
        self.assertEqual(browser.call.await_count, 1)
        self.assertEqual([job.source_job_id for job in resumed['items']], ['456'])
        self.assertEqual(resumed['source_results'][0]['total_declared'], 2)
        self.assertTrue(resumed['source_results'][0]['baseline_eligible'])
        self.assertFalse(resumed['can_continue'])

    async def test_later_page_errors_never_qualify_partial_rows_for_baseline(self):
        for codes in ([], [area_catalog()[0]['code']]):
            for status in ('blocked', 'failed'):
                with self.subTest(codes=codes, status=status):
                    calls = []
                    async def provider(name, args):
                        calls.append(args['page'])
                        if args['page'] == 2:
                            raise SourceError(status)
                        payload, raw = await fake104(name, args)
                        return payload | {'has_next': True, 'total': 10}, raw
                    result = await Sources(mcp_call=provider).search(DailySearchInput(
                        criteria={'keyword': 'planner', 'area_codes': codes}, sources=['104']))
                    diagnostic = result['source_results'][0]
                    self.assertEqual(calls, [1, 2])
                    self.assertEqual(len(result['items']), 1)
                    self.assertEqual(diagnostic['status'], 'partial')
                    self.assertFalse(diagnostic['baseline_eligible'])
                    self.assertFalse(diagnostic['truncated'])
                    self.assertFalse(diagnostic['quota_limited'])
                    self.assertTrue(result['can_continue'])

    async def test_source_empty_after_filtering_does_not_claim_area_declared_zero(self):
        for source in ('104', '518', '1111'):
            with self.subTest(source=source):
                body = SEARCH_HTML.replace('<link rel="next" href="/job-index-P-2.html?ad=%E6%8E%A1%E8%B3%BC&amp;aa=3001008">', '')
                browser = SimpleNamespace(call=AsyncMock(return_value=page()))
                service = Sources(httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
                                  fake104, browser1111=browser, interval=0)
                result = await service.search(DailySearchInput(criteria={'keyword': 'planner'}, sources=[source]),
                                              exclude_ids={'12', 'A1', '123'})
                diagnostic = result['source_results'][0]
                self.assertEqual(result['items'], [])
                self.assertEqual(diagnostic['status'], 'empty')
                self.assertTrue(diagnostic['baseline_eligible'])
                self.assertFalse(diagnostic['truncated'])
                self.assertFalse(diagnostic['quota_limited'])
                self.assertEqual(result['area_results'][0]['status'], 'success')
                self.assertTrue(result['area_results'][0]['completed'])

    async def test_empty_518_source_total_stays_unknown_while_area_is_declared_empty(self):
        result = await Sources(httpx.MockTransport(lambda request: httpx.Response(200,
            text='<div id="listContent">查無符合條件的職缺</div>')), interval=0).search(
                DailySearchInput(criteria={'keyword': 'planner'}, sources=['518']))
        diagnostic = result['source_results'][0]
        self.assertEqual(diagnostic['status'], 'empty')
        self.assertIsNone(diagnostic['total_declared'])
        self.assertTrue(diagnostic['baseline_eligible'])
        self.assertEqual(result['area_results'][0]['total_declared'], 0)
        self.assertEqual(result['area_results'][0]['status'], 'empty')

    async def test_worker_deferred_later_page_is_budget_limited_and_ineligible(self):
        for codes in ([], [area_catalog()[0]['code']]):
            with self.subTest(codes=codes):
                name = area_catalog()[0]['name'] if codes else ''
                browser = SimpleNamespace(call=AsyncMock(side_effect=[page(total=2),
                    dict(status='ok', areas=[dict(area_name=name, status='time_limited', message='budget')])]))
                args = DailySearchInput(criteria={'keyword': 'planner', 'area_codes': codes}, sources=['1111'])
                result = await Sources(browser1111=browser).search(args)
                diagnostic = result['source_results'][0]
                self.assertEqual(len(result['items']), 1)
                self.assertEqual(diagnostic['status'], 'partial')
                self.assertTrue(diagnostic['truncated'])
                self.assertTrue(diagnostic['quota_limited'])
                self.assertFalse(diagnostic['baseline_eligible'])
                self.assertEqual(json.loads(result['continuation'])['sources']['1111']['areas'][codes[0] if codes else 'all']['next_page'], 2)

    async def test_104_deadline_boundary_reserve_and_no_deadline(self):
        for codes in ([], [area_catalog()[0]['code']]):
            for deadline, expected in ((None, 1), (134.999, 0), (135, 1)):
                with self.subTest(codes=codes, deadline=deadline):
                    provider = AsyncMock(side_effect=fake104)
                    with patch('app.sources.time.monotonic', return_value=100):
                        result = await Sources(mcp_call=provider).search(DailySearchInput(
                            criteria={'keyword': 'planner', 'area_codes': codes}, sources=['104']),
                            deadline=deadline, finish_reserve=5)
                    self.assertEqual(provider.await_count, expected)
                    diagnostic = result['source_results'][0]
                    self.assertEqual(diagnostic['baseline_eligible'], bool(expected))
                    self.assertEqual(diagnostic['quota_limited'], not expected)
                    self.assertEqual(diagnostic['truncated'], not expected)
                    self.assertEqual(result['area_results'][0]['status'], 'success' if expected else 'not_queried')
                    if expected:
                        self.assertEqual(provider.call_args.args[1]['area'], [area_catalog()[0]['name']] if codes else [])

    async def test_http_area_round_never_exceeds_per_source_page_cap(self):
        areas = area_catalog()[:3]
        calls = []
        async def provider(name, args):
            calls.append((args['area'][0], args['page']))
            ident = str(len(calls))
            return dict(jobs=[dict(job_no=ident, job_name='planner', company='company',
                                  url='https://www.104.com.tw/job/a'+ident)], total=100, has_next=True), None
        result = await Sources(mcp_call=provider).search(DailySearchInput(
            criteria={'keyword': 'planner', 'area_codes': [area['code'] for area in areas]}, sources=['104']))
        self.assertEqual(calls, [(area['name'], 1) for area in areas] + [(area['name'], 2) for area in areas[:2]])
        diagnostic = result['source_results'][0]
        self.assertEqual(diagnostic['pages_fetched'], 5)
        self.assertTrue(diagnostic['baseline_eligible'])
        self.assertTrue(diagnostic['truncated'])
        self.assertTrue(diagnostic['quota_limited'])



class R4SourceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_queued_areas_finish_without_sticky_partial(self):
        for count, size, warned, empty in ((3, 7, False, False), (2, 12, False, True), (4, 8, True, False)):
            with self.subTest(count=count, size=size, warned=warned, empty=empty):
                areas = area_catalog()[:count]
                calls = []
                async def provider(name, args):
                    index = [area['name'] for area in areas].index(args['area'][0])
                    calls.append(index)
                    jobs = [] if empty and index == 0 else [dict(job_no=str(index * 100 + n), job_name='planner',
                        company='synthetic', url=f'https://www.104.com.tw/job/a{index}b{n}') for n in range(size)]
                    if warned and index == 0:
                        jobs.append(None)
                    return dict(jobs=jobs, total=0 if empty and index == 0 else size, has_next=False), None
                service = Sources(mcp_call=provider)
                args = SearchInput(criteria=dict(keyword='planner', area_codes=[area['code'] for area in areas]), sources=['104'], limit=1)
                result = await service.search(args)
                self.assertTrue(any(area['status'] == 'partial' and not area['completed'] for area in result['area_results']))
                identifiers = [job.source_job_id for job in result['items']]
                turns = 1
                saved_statuses = []
                while result['can_continue'] and turns <= count * size:
                    cursor = json.loads(result['continuation'])
                    saved_statuses.extend(state['status'] for state in cursor['sources']['104']['areas'].values())
                    result = await service.search(args, continuation=cursor)
                    identifiers.extend(job.source_job_id for job in result['items'])
                    turns += 1
                self.assertFalse(result['can_continue'])
                self.assertEqual(len(identifiers), size * (count - int(empty)))
                self.assertEqual(len(set(identifiers)), len(identifiers))
                self.assertEqual(calls, list(range(count)))
                self.assertEqual([area['completed'] for area in result['area_results']], [True] * count)
                expected = ['empty' if empty and i == 0 else 'partial' if warned and i == 0 else 'success' for i in range(count)]
                self.assertEqual([area['status'] for area in result['area_results']], expected)
                self.assertEqual(result['source_results'][0]['status'], 'partial' if warned else 'success')
                self.assertNotIn('partial', saved_statuses)
                if warned:
                    self.assertIn('格式不完整', result['area_results'][0]['message'])

    async def test_1111_normalization_failure_is_local_and_retryable(self):
        for bad_index, field in ((1, 'title'), (0, 'company')):
            with self.subTest(bad_index=bad_index, field=field):
                areas = area_catalog()[:3]
                async def worker(arguments):
                    results = []
                    for name in arguments['area_names']:
                        index = [area['name'] for area in areas].index(name)
                        candidate = row(str(101 + index))
                        if index == bad_index and browser.call.await_count == 1:
                            candidate[field] = 'x' * 501
                        results.append(dict(page([candidate]), area_name=name))
                    return dict(status='ok', areas=results)
                browser = SimpleNamespace(call=AsyncMock(side_effect=worker))
                service = Sources(browser1111=browser)
                args = SearchInput(criteria=dict(keyword='planner', area_codes=[a['code'] for a in areas]), sources=['1111'])
                result = await service.search(args)
                self.assertEqual([job.source_job_id for job in result['items']], [str(101 + i) for i in range(3) if i != bad_index])
                self.assertEqual([area['status'] for area in result['area_results']], ['failed' if i == bad_index else 'success' for i in range(3)])
                self.assertTrue(result['area_results'][bad_index]['message'])
                self.assertTrue(result['can_continue'])
                self.assertFalse(result['source_results'][0]['baseline_eligible'])
                states = json.loads(result['continuation'])['sources']['1111']['areas']
                self.assertEqual([states[a['code']]['next_page'] for a in areas], [1 if i == bad_index else 2 for i in range(3)])
                resumed = await service.search(args, continuation=result['continuation'])
                self.assertEqual(browser.call.call_args.args[0]['area_names'], [areas[bad_index]['name']])
                self.assertEqual(browser.call.call_args.args[0]['page'], 1)
                self.assertEqual([job.source_job_id for job in resumed['items']], [str(101 + bad_index)])
                self.assertFalse(resumed['can_continue'])

    async def test_518_reload_only_touches_unfinished_mapped_areas(self):
        areas = area_catalog()[:2]
        mapping = {area['name']: str(3001001 + i) for i, area in enumerate(areas)}
        async def first_request(method, url, **kwargs):
            ident = 'A1' if mapping[areas[0]['name']] in url else 'B1'
            link = '<link rel="next" href="/job-index-P-2.html?ad=%E6%8E%A1%E8%B3%BC&amp;aa=3001008">'
            body = SEARCH_HTML.replace(link, '').replace('A1', ident)
            if ident != 'A1':
                body = body.replace('</head>', link.replace('3001008', mapping[areas[1]['name']]) + '</head>')
                card = body.split('<div id="listContent">', 1)[1].rsplit('  </div>', 1)[0]
                body = body.replace(card, ''.join(card.replace('B1', f'B{i}') for i in range(1, 11)))
            return httpx.Response(200, text=body)
        service = Sources(interval=0)
        service.regions518 = mapping
        service.request = AsyncMock(side_effect=first_request)
        args = SearchInput(criteria=dict(keyword='採購', area_codes=[a['code'] for a in areas]), sources=['518'], limit=5)
        first = await service.search(args)
        self.assertEqual([a['completed'] for a in first['area_results']], [True, False])
        for mode in ('failed', 'budget', 'all_done'):
            with self.subTest(mode=mode):
                cursor = json.loads(first['continuation'])
                if mode == 'all_done':
                    cursor['sources']['518']['areas'][areas[1]['code']]['has_next'] = False
                requests = []
                def handler(request):
                    requests.append(str(request.url))
                    return httpx.Response(500)
                fresh = Sources(httpx.MockTransport(handler), interval=0)
                kwargs = dict(deadline=101, finish_reserve=0) if mode == 'budget' else {}
                with patch('app.sources.time.monotonic', return_value=100):
                    result = await fresh.search(args, continuation=cursor, **kwargs)
                self.assertEqual(result['area_results'][0], first['area_results'][0])
                if mode == 'all_done':
                    self.assertEqual(requests, [])
                    self.assertFalse(result['can_continue'])
                    self.assertEqual([a['status'] for a in result['area_results']], ['success', 'success'])
                    self.assertTrue(result['source_results'][0]['baseline_eligible'])
                else:
                    self.assertTrue(result['can_continue'])
                    self.assertEqual(result['area_results'][1]['status'], 'partial')
                    self.assertFalse(result['area_results'][1]['completed'])
                    self.assertEqual(len(requests), 0 if mode == 'budget' else 2)
                    state = json.loads(result['continuation'])['sources']['518']['areas'][areas[1]['code']]
                    if mode == 'failed':
                        self.assertEqual(state['status'], 'failed')
                    self.assertEqual(state['next_page'], 2)
                    self.assertIsNotNone(state['next_url'])

    async def test_http_block_stops_source_but_failure_remains_local(self):
        for source in ('104', '518'):
            for error in ('blocked', 'failed'):
                with self.subTest(source=source, error=error):
                    areas = area_catalog()[:5]
                    calls = []
                    async def fetch(name, args):
                        calls.append(args['area'][0])
                        if len(calls) == 2:
                            raise SourceError(error)
                        ident = str(len(calls))
                        return dict(jobs=[dict(job_no=ident, job_name='planner', company='synthetic',
                            url='https://www.104.com.tw/job/a'+ident)], total=1, has_next=False), None
                    async def request(method, url, **kwargs):
                        calls.append(url)
                        if len(calls) == 2:
                            raise SourceError(error)
                        body = SEARCH_HTML.replace('<link rel="next" href="/job-index-P-2.html?ad=%E6%8E%A1%E8%B3%BC&amp;aa=3001008">', '')
                        return httpx.Response(200, text=body.replace('A1', 'A'+str(len(calls))))
                    service = Sources(mcp_call=fetch, interval=0)
                    service.regions518 = {a['name']: str(3001001+i) for i,a in enumerate(areas)}
                    service.request = AsyncMock(side_effect=request)
                    args = SearchInput(criteria=dict(keyword='planner', area_codes=[a['code'] for a in areas]), sources=[source])
                    result = await service.search(args)
                    self.assertEqual(len(calls), 2 if error == 'blocked' else 5)
                    self.assertEqual([a['status'] for a in result['area_results']], ['success', error] + (['not_queried'] * 3 if error == 'blocked' else ['success'] * 3))
                    self.assertEqual(len(result['items']), 1 if error == 'blocked' else 4)
                    self.assertTrue(result['can_continue'])

    async def test_1111_blocked_area_prevents_later_worker_batches(self):
        areas = area_catalog()[:3]
        for error in ('blocked', 'failed'):
            with self.subTest(error=error):
                async def worker(arguments):
                    results = []
                    for name in arguments['area_names']:
                        index = [a['name'] for a in areas].index(name)
                        if index == 1:
                            results.append(dict(area_name=name, status=error, message='synthetic failure'))
                        else:
                            number = arguments['page']
                            results.append(dict(page([row(str(101+index+number*10))], number=number, total=2), area_name=name))
                    return dict(status='ok', areas=results)
                browser = SimpleNamespace(call=AsyncMock(side_effect=worker))
                result = await Sources(browser1111=browser).search(SearchInput(
                    criteria=dict(keyword='planner', area_codes=[a['code'] for a in areas]), sources=['1111']))
                self.assertEqual(browser.call.await_count, 1 if error == 'blocked' else 2)
                self.assertEqual(len(result['items']), 2 if error == 'blocked' else 4)
                self.assertEqual(result['area_results'][1]['status'], error)

    async def test_corrupted_v2_cursor_uses_existing_invalid_messages(self):
        provider = AsyncMock(return_value=(dict(jobs=[dict(job_no='1', job_name='planner', company='synthetic', url='https://www.104.com.tw/job/a1')], total=2, has_next=True), None))
        service = Sources(mcp_call=provider)
        args = SearchInput(criteria=dict(keyword='planner'), sources=['104'], limit=1)
        first = await service.search(args)
        for field, value, message in [('status', [], '續查區域狀態無效，請從原搜尋重新開始。'),
                                      ('status', {}, '續查區域狀態無效，請從原搜尋重新開始。'),
                                      ('status', 'partial', '續查區域狀態無效，請從原搜尋重新開始。'),
                                      *[(field, float('inf'), '續查頁碼無效，請從原搜尋重新開始。') for field in ('next_page', 'pages', 'returned_count')],
                                      ('total_declared', 'missing', '續查區域狀態無效，請從原搜尋重新開始。')]:
            with self.subTest(field=field, value=value):
                cursor = json.loads(first['continuation'])
                state = cursor['sources']['104']['areas']['all']
                if value == 'missing':
                    del state[field]
                    state['remaining'] = [{}]
                else:
                    state[field] = value
                result = await service.search(args, continuation=json.dumps(cursor))
                self.assertEqual(result['items'], [])
                self.assertEqual(result['source_results'][0]['status'], 'unsupported')
                self.assertEqual(result['source_results'][0]['message'], message)
                self.assertFalse(result['can_continue'])
                self.assertEqual(provider.await_count, 1)


class R4ReferenceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_historical_areas_are_compact_current_tool_areas_stay_complete(self):
        for status, completed in (('partial', True), ('blocked', False)):
            with self.subTest(status=status, completed=completed):
                area = dict(area_code='synthetic', area_name='合成地區', source='104', status=status,
                    returned_count=2, total_declared=3, message='synthetic warning', completed=completed)
                data = dict(search_id='search', criteria=dict(keyword='planner'), can_continue=True, area_results=[area], items=[])
                store = MagicMock()
                db = store.connect.return_value.__enter__.return_value
                db.execute.return_value.fetchone.side_effect = [dict(seq=3, conversation_id='conversation'), [json.dumps(dict(text='continue'))], None, [1]]
                store.messages.return_value = dict(items=[dict(request_id='old', role='assistant', content=dict(status='completed', text='old', events=[dict(event='jobs', data=data)]))])
                store.conversation_state.return_value = dict(shortlist=[], excluded_companies=[], removed=[], dismissed_ids=[])
                store.conversation_user_texts.return_value = []
                captured = []
                async def model(client, settings, key, protocol, messages, tools, **kwargs):
                    captured.extend(copy.deepcopy(messages))
                    yield dict(kind='text_delta', text='done')
                app = SimpleNamespace(state=SimpleNamespace(store=store, active_chats={}, llm_transport=httpx.MockTransport(lambda req: httpx.Response(500))))
                queue = asyncio.Queue()
                invoke = AsyncMock(return_value=(data, ('jobs', data)))
                with patch('app.chat.stream_model', side_effect=model):
                    await produce_reply(app, 'current', {}, 'synthetic', 'responses', [], lambda call: SearchInput(criteria=dict(keyword='planner'), sources=['104']), invoke, 'auth', queue, initial_search=dict(search_id='search'))
                references = [json.loads(message['content'].split('\n', 1)[1]) for message in captured if message['role'] == 'user' and isinstance(message['content'], str) and message['content'].startswith('以下是這段對話已保存的職缺引用')]
                self.assertEqual(len(references), 1)
                self.assertEqual(references[0][0]['area_results'], [{key: area[key] for key in ('area_name', 'source', 'status', 'completed')}])
                tool_results = [json.loads(message['content']) for message in captured if message['role'] == 'tool']
                self.assertEqual(len(tool_results), 1)
                self.assertEqual(tool_results[0]['area_results'], [area])
                self.assertEqual(store.save_reply.call_args.args[1]['status'], 'completed')


