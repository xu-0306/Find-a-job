"""Offline 1111 contract, switches, continuation and durable compatibility."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch

from app import backups, security
from app.backup_verify import validate_backup
from app.daily import commit_results
from app.source1111 import Browser1111, BrowserError, search_rows, SINGLE_AREA_SECONDS, STARTUP_SECONDS, WORKER_GRACE_SECONDS, call_seconds
from app.sources import Sources, SearchInput, enabled_sources
from app.store import Job, Store, encode, now
from test_m2_chat import ChatChecks, SETTINGS, events, response
from test_m2_sources import fake104


def row(ident='123'):
    return dict(id=ident, title='品管工程師', company='合成製造公司', url=f'https://www.1111.com.tw/job/{ident}',
                companyUrl='https://www.1111.com.tw/corp/987/', text='公開摘要 test@example.invalid')


def page(rows=None, number=1, size=1, total=1):
    return dict(status='ok', jobs=[row()] if rows is None else rows, page=number, page_size=size,
                total=total, has_next=number * size < total, warnings=[])


def job1111():
    return Job(source='1111', source_job_id='123', title='品管工程師', company_name='合成製造公司',
               company_source_id='987', url='https://www.1111.com.tw/job/123/')


class AdapterChecks(unittest.IsolatedAsyncioTestCase):

    async def test_startup_admission_boundary_search_and_no_process_after_lock_wait(self):
        # Without F1, a budget just below the admission threshold starts a browser even though startup leaves too little area time.
        threshold = SINGLE_AREA_SECONDS + STARTUP_SECONDS + WORKER_GRACE_SECONDS
        args = SearchInput(criteria={'keyword': '品管'}, sources=['1111'])
        for budget, admitted in [(threshold - .01, False), (threshold, True), (threshold + .01, True)]:
            browser = AsyncMock(); browser.call.return_value = page()
            with patch('app.sources.time.monotonic', return_value=100):
                result = await Sources(browser1111=browser).search(args, deadline=100 + budget)
            self.assertEqual(browser.call.await_count, int(admitted))
            self.assertEqual(result['source_results'][0]['status'], 'success' if admitted else 'failed')
            if not admitted: self.assertEqual(result['area_results'][0]['status'], 'not_queried')
            if not admitted:
                self.assertTrue(result['can_continue'])
        bridge = Browser1111()
        clock = Mock(); clock.monotonic.side_effect = [100, 133]
        with patch('app.source1111.time', clock), \
                patch('app.source1111.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            with self.assertRaises(BrowserError) as caught:
                await bridge.call({'area_names': []}, timeout=100)
        self.assertEqual(caught.exception.status, 'time_limited')
        self.assertIn('時間不足', caught.exception.message)
        spawn.assert_not_awaited()
        self.assertFalse(bridge.lock.locked())

    async def test_queued_single_area_with_caller_budget_starts_worker(self):
        await self.check_queued_single_area(240, 3, 64000)

    async def test_queue_wait_can_exceed_the_single_execution_cap(self):
        await self.check_queued_single_area(240, 80, 64000)

    async def test_queue_without_caller_deadline_gets_a_full_execution_cap(self):
        await self.check_queued_single_area(None, 3, 64000)

    async def test_queue_with_insufficient_remaining_budget_never_spawns(self):
        await self.check_queued_single_area(75, 3, None)

    async def check_queued_single_area(self, timeout, wait, expected_ms):
        # Real coroutine contention; only elapsed time is simulated, never lock acquisition.
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = 0; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        process.stdout.read.side_effect = [b'{"status":"ok"}', b'']
        elapsed = 100
        clock = Mock(); clock.monotonic.side_effect = lambda: elapsed
        held, release = asyncio.Event(), asyncio.Event()
        async def holder():
            async with bridge.lock:
                held.set()
                await release.wait()
        async def launch(*args, **kwargs):
            nonlocal elapsed
            elapsed += 1  # Node spawn also consumes worker budget.
            return process
        holding = asyncio.create_task(holder())
        await held.wait()
        with patch('app.source1111.time', clock), \
                patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', side_effect=launch) as spawn:
            pending = asyncio.create_task(bridge.call({'action': 'search', 'area_names': []}, timeout=timeout))
            try:
                await asyncio.sleep(0)
                self.assertFalse(pending.done())
                self.assertTrue(bridge.lock.locked())
                spawn.assert_not_awaited()
                elapsed += wait
            finally:
                release.set()
                await holding
            if expected_ms is None:
                with self.assertRaises(BrowserError) as caught:
                    await pending
                self.assertEqual(caught.exception.status, 'time_limited')
                spawn.assert_not_awaited()
            else:
                self.assertEqual(await pending, {'status': 'ok'})
                spawn.assert_awaited_once()
                payload = json.loads(process.stdin.write.call_args.args[0])
                self.assertEqual(payload['budget_ms'], expected_ms)
                if timeout is not None:
                    self.assertLessEqual(wait * 1000 + 1000 + payload['budget_ms'] + WORKER_GRACE_SECONDS * 1000, timeout * 1000)
        self.assertFalse(bridge.lock.locked())

    async def test_single_area_cap_allows_startup_when_caller_has_more_time(self):
        # Without the cap reset, a single-area call loses 1s before admission and never starts.
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = 0; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        process.stdout.read.side_effect = [b'{"status":"ok"}', b'']
        clock = Mock(); clock.monotonic.side_effect = [100, 101, 101]
        with patch('app.source1111.time', clock), \
                patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process) as spawn:
            await bridge.call({'area_names': []}, timeout=340)
        spawn.assert_awaited_once()
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0])['budget_ms'], 65000)

    async def test_lock_timeout_keeps_same_page_and_cursor_without_browser_stop_message(self):
        # Without F6 the bridge says a browser was stopped and Sources lacks partial/quota diagnostics.
        bridge = Browser1111()
        await bridge.lock.acquire()
        with patch('app.source1111.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            with self.assertRaises(BrowserError) as caught:
                await bridge.call({'area_names': []}, timeout=.001)
        self.assertEqual(caught.exception.status, 'time_limited')
        self.assertIn('時間不足', caught.exception.message)
        self.assertNotIn('已停止', caught.exception.message)
        spawn.assert_not_awaited()
        bridge.lock.release()
        browser = AsyncMock(); browser.call.side_effect = caught.exception
        args = SearchInput(criteria={'keyword': '設計'}, sources=['104', '1111'])
        source = Sources(mcp_call=fake104, browser1111=browser)
        result = await source.search(args)
        diag = result['source_results'][1]
        self.assertEqual(diag['status'], 'partial')
        self.assertTrue(diag['truncated']); self.assertTrue(diag['quota_limited'])
        self.assertFalse(diag['baseline_eligible']); self.assertTrue(result['can_continue'])
        self.assertIn('時間不足', diag['message']); self.assertNotIn('已停止', diag['message'])
        state = json.loads(result['continuation'])['sources']['1111']
        self.assertEqual(state['areas']['all']['next_page'], 1); self.assertEqual(state['parser_warnings'], [])
        browser.call.side_effect = None; browser.call.return_value = page()
        resumed = await source.search(args, continuation=result['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 1)
        self.assertEqual(resumed['source_results'][1]['status'], 'success')

    async def test_unsupported_deferred_region_preserves_other_regions_next_page(self):
        # Without F3, unsupported clears has_next and silently loses 台南市 page 2.
        browser = AsyncMock(); browser.call.side_effect = [dict(status='ok', areas=[
            dict(page(total=2), area_name='台南市'),
            dict(area_name='高雄市', status='time_limited', message='時間不足')]),
            BrowserError('unsupported', '地區無法對應'), page([row('456')], number=2, total=2)]
        source = Sources(browser1111=browser)
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}, sources=['1111'], limit=1)
        first = await source.search(args)
        second = await source.search(args, continuation=first['continuation'])
        self.assertEqual([(call.args[0]['area_names'], call.args[0]['page']) for call in browser.call.call_args_list],
                         [(['台南市', '高雄市'], 1), (['高雄市'], 1), (['台南市'], 2)])
        self.assertFalse(second['source_results'][0]['baseline_eligible'])
        self.assertIn('高雄市', second['source_results'][0]['message'])
        self.assertEqual([row['status'] for row in second['area_results']], ['success', 'unsupported'], second['area_results'])
        self.assertEqual(second['area_results'][1]['area_name'], '高雄市')
        self.assertEqual(browser.call.call_args.args[0]['area_names'], ['台南市'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 2)
        self.assertEqual([job.source_job_id for job in second['items']], ['456'])
        self.assertFalse(second['can_continue'])

    async def test_daily_budget_five_pages_and_multi_area_ceiling(self):
        # All five pages must fit even if each consumes the full single-area execution cap.
        from app.scheduler import daily_1111_seconds, DAILY_1111_SECONDS
        from app.sources import DailySearchInput
        args = DailySearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000']}, sources=['1111'])
        self.assertEqual(daily_1111_seconds(args.criteria), call_seconds(1) * 5)
        self.assertEqual(daily_1111_seconds(args.criteria.model_copy(update={'area_codes': ['a', 'b']})), 590)
        self.assertEqual(daily_1111_seconds(args.criteria.model_copy(update={'area_codes': ['a', 'b', 'c']})), DAILY_1111_SECONDS)
        self.assertEqual(DAILY_1111_SECONDS, 600)
        elapsed = 100
        async def query(arguments, **kwargs):
            nonlocal elapsed
            self.assertGreaterEqual(kwargs['timeout'], call_seconds(1))
            elapsed += call_seconds(1)
            number = arguments['page']
            return page([row(str(i)) for i in range((number - 1) * 20 + 1, number * 20 + 1)], number=number, size=20, total=100)
        browser = AsyncMock(); browser.call.side_effect = query
        clock = Mock(); clock.monotonic.side_effect = lambda: elapsed
        with patch('app.sources.time', clock):
            result = await Sources(browser1111=browser).search(args, deadline=100 + daily_1111_seconds(args.criteria))
        self.assertEqual(browser.call.await_count, 5); self.assertEqual(len(result['items']), 100)
        self.assertTrue(result['source_results'][0]['baseline_eligible'])
        self.assertEqual(result['source_results'][0]['status'], 'success'); self.assertFalse(result['can_continue'])
        # Three areas hit 600s; slow page batches stay partial and never become baseline-eligible.
        multi = args.model_copy(update={'criteria': args.criteria.model_copy(update={
            'area_codes': ['6001014000', '6001016000', '6001001000']})})
        elapsed = 100
        async def slow_query(arguments, **kwargs):
            nonlocal elapsed
            elapsed += 300
            number = arguments['page']
            self.assertEqual(len(arguments['area_names']), 3)
            return dict(status='ok', areas=[dict(
                page([row(str(i)) for i in range((number - 1) * 20 + 1, number * 20 + 1)], number=number, size=20, total=100),
                area_name=name) for name in arguments['area_names']])
        browser.call.side_effect = slow_query; browser.call.reset_mock()
        with patch('app.sources.time', clock):
            result = await Sources(browser1111=browser).search(multi, deadline=100 + daily_1111_seconds(multi.criteria))
        self.assertEqual(browser.call.await_count, 2); self.assertEqual(len(result['items']), 40)
        self.assertEqual(result['source_results'][0]['status'], 'partial')
        self.assertFalse(result['source_results'][0]['baseline_eligible']); self.assertTrue(result['can_continue'])

    async def test_daily_five_pages_commit_an_initial_baseline(self):
        # Without F2, page 4 is skipped and commit_results refuses to create the 1111 baseline.
        from app.scheduler import DailyRunner
        elapsed = 100
        async def query(arguments, **kwargs):
            nonlocal elapsed
            elapsed += 25
            number = arguments['page']
            return page([row(str(i)) for i in range((number - 1) * 20 + 1, number * 20 + 1)], number=number, size=20, total=100)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect(write=True) as db:
                store.put(db, 'default_preferences', [{'keyword': '品管', 'area_codes': ['6001014000']}])
                store.put(db, 'services', {'source_518_enabled': False, 'source_1111_enabled': True})
            browser = AsyncMock(); browser.call.side_effect = query
            clock = Mock(); clock.monotonic.side_effect = lambda: elapsed
            with patch('app.scheduler.time', clock), patch('app.sources.time', clock):
                runner = DailyRunner(store, Sources(mcp_call=fake104, browser1111=browser), automatic=False)
                ident = runner.start('manual'); await runner.task
            self.assertEqual(browser.call.await_count, 5)
            with store.connect() as db:
                result = json.loads(db.execute('SELECT result_json FROM runs WHERE id=?', (ident,)).fetchone()[0])
                diag = next(item for item in result['source_results'] if item['source'] == '1111')
                self.assertEqual(diag['status'], 'success'); self.assertTrue(diag['baseline'])
                self.assertTrue(diag['completed'])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM preference_baselines WHERE source='1111'").fetchone()[0], 1)

    def test_worker_and_python_budget_constants_match(self):
        # Without F1, the worker's old MAX_BUDGET/defaults diverge from Python's execution cap.
        import re
        from app import source1111
        worker = (Path(__file__).resolve().parents[1] / 'experiments/1111-playwright/source.mjs').read_text(encoding='utf-8')
        for js, py, factor in [('SINGLE_AREA_MS', 'SINGLE_AREA_SECONDS', 1000), ('STARTUP_MS', 'STARTUP_SECONDS', 1000),
                               ('WORKER_GRACE_MS', 'WORKER_GRACE_SECONDS', 1000), ('MAX_AREAS', 'MAX_AREAS', 1),
                               ('MAX_SEARCH_PAGES', 'MAX_SEARCH_PAGES', 1)]:
            self.assertEqual(int(re.search(r'export const ' + js + r' = (\d+);', worker)[1]), getattr(source1111, py) * factor)
        self.assertIn('MAX_BUDGET_MS = STARTUP_MS + SINGLE_AREA_MS * MAX_AREAS;', worker)
        self.assertIn('timeout: LAUNCH_TIMEOUT_MS', worker)
        self.assertEqual(int(re.search(r'export const LAUNCH_TIMEOUT_MS = (\d+);', worker)[1]), 15000)
        self.assertEqual(STARTUP_SECONDS, 20)
        self.assertEqual(call_seconds(source1111.MAX_AREAS) - WORKER_GRACE_SECONDS, 470)

    async def test_multi_area_merge_deduplicates_and_only_continues_unfinished_area(self):
        browser = AsyncMock()
        browser.call.side_effect = [dict(status='ok', areas=[
            dict(page([row('123'), *[row(str(800 + i)) for i in range(8)]], size=9, total=9), area_name='台南市'),
            dict(page([row('123'), row('456')], size=2, total=3), area_name='高雄市')]),
            page([row('789')], number=2, size=2, total=3)]
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}, sources=['1111'], limit=5)
        source = Sources(browser1111=browser)
        first = await source.search(args)
        self.assertEqual([job.source_job_id for job in first['items']], ['123', '456', *[str(800 + i) for i in range(8)]])
        self.assertEqual(first['duplicate_count'], 1)
        self.assertEqual(first['source_results'][0]['total_declared'], 12)
        self.assertTrue(first['can_continue'])
        second = await source.search(args, continuation=first['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['area_names'], ['高雄市'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 2)
        self.assertEqual([job.source_job_id for job in second['items']], ['789'])
        self.assertEqual(second['source_results'][0]['total_declared'], 12)
        self.assertFalse(second['can_continue'])

    async def test_partial_area_failure_and_invalid_payload_are_never_empty_success(self):
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}, sources=['1111'])
        for failure in [dict(status='blocked', message='要求人機驗證'), page([], total=2),
                        page([row() | {'companyUrl': 'https://other.invalid/corp/1'}])]:
            with self.subTest(failure=failure):
                browser = AsyncMock()
                browser.call.return_value = dict(status='ok', areas=[dict(page(), area_name='台南市'), dict(failure, area_name='高雄市')])
                result = await Sources(browser1111=browser).search(args)
                diagnostic = result['source_results'][0]
                self.assertEqual(diagnostic['status'], 'partial')
                self.assertFalse(diagnostic['baseline_eligible'])
                self.assertIsNone(diagnostic['total_declared'])
                self.assertIn('高雄市', diagnostic['message'])
                self.assertEqual(len(result['items']), 1)

    async def test_all_regions_fail_with_explicit_source_status(self):
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}, sources=['1111'])
        for status in ['blocked', 'unsupported', 'failed']:
            browser = AsyncMock()
            browser.call.return_value = dict(status='ok', areas=[dict(area_name=name, status=status, message='合成失敗原因') for name in ['台南市', '高雄市']])
            result = await Sources(browser1111=browser).search(args)
            self.assertEqual(result['source_results'][0]['status'], status)
            self.assertEqual(result['items'], [])
            self.assertIn('台南市', result['source_results'][0]['message'])
            self.assertIn('高雄市', result['source_results'][0]['message'])

    async def test_worker_area_success_failure_and_time_limited_are_preserved_and_resumed(self):
        from app.sources import area_catalog
        areas = area_catalog()[:3]
        names = [item['name'] for item in areas]
        browser = AsyncMock()
        browser.call.side_effect = [
            dict(status='ok', areas=[dict(page([row('123')]), area_name=names[0]),
                dict(area_name=names[1], status='blocked', message='要求驗證'),
                dict(area_name=names[2], status='time_limited', message='時間不足')]),
            dict(status='ok', areas=[dict(page([row('456')]), area_name=names[1]),
                dict(page([row('789')]), area_name=names[2])]),
        ]
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': [item['code'] for item in areas]},
            sources=['1111'], limit=20)
        source = Sources(browser1111=browser)
        first = await source.search(args)
        self.assertEqual([row['status'] for row in first['area_results']], ['success', 'blocked', 'not_queried'])
        self.assertEqual([row['completed'] for row in first['area_results']], [True, False, False])
        self.assertTrue(first['can_continue'])
        resumed = await source.search(args, continuation=first['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['area_names'], names[1:])
        self.assertEqual(browser.call.call_args.args[0]['page'], 1)
        self.assertEqual([row['status'] for row in resumed['area_results']], ['success', 'success', 'success'])
        self.assertEqual([row['returned_count'] for row in resumed['area_results']], [1, 1, 1])
        self.assertFalse(resumed['can_continue'])

    async def test_old_v1_single_area_cursor_maps_to_area_state(self):
        for region_fields in (False, True):
            browser = AsyncMock()
            browser.call.side_effect = [page(total=2), page([row('456')], number=2, total=2)]
            args = SearchInput(criteria={'keyword': '品管'}, sources=['1111'], limit=1)
            source = Sources(browser1111=browser)
            first = await source.search(args)
            cursor = json.loads(first['continuation'])
            cursor['version'] = 1
            state = cursor['sources']['1111']
            area = state['areas']['all']
            cursor['sources']['1111'] = dict(page_size=state['page_size'], next_page=area['next_page'],
                next_url=area['next_url'], remaining=area['remaining'], seen_ids=state['seen_ids'], pages=area['pages'],
                visited=area['visited'], page_signatures=area['page_signatures'], has_next=area['has_next'],
                total_declared=area['total_declared'], parser_warnings=state['parser_warnings'],
                pending_next_areas=[])
            if region_fields:
                cursor['sources']['1111'].update(active_areas=[''], area_totals={'': area['total_declared']})
            second = await source.search(args, continuation=json.dumps(cursor, ensure_ascii=False))
            self.assertEqual([job.source_job_id for job in second['items']], ['456'])
            self.assertEqual(browser.call.call_args.args[0]['page'], 2)

    async def test_deadline_skips_browser_and_preserves_continuation_without_stale_warning(self):
        browser = AsyncMock()
        result = await Sources(mcp_call=fake104, browser1111=browser).search(
            SearchInput(criteria={'keyword': '設計'}, sources=['104', '1111']), deadline=time.monotonic() + 46, finish_reserve=45)
        browser.call.assert_not_awaited()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['area_results'][0]['status'], 'not_queried')
        diagnostic = result['source_results'][1]
        self.assertEqual(diagnostic['status'], 'failed')
        self.assertIn('時間不足', diagnostic['message'])
        self.assertTrue(diagnostic['truncated'])
        self.assertTrue(diagnostic['quota_limited'])
        self.assertNotIn('時間不足', encode(result['filter_warnings']))
        self.assertFalse(diagnostic['baseline_eligible'])
        self.assertTrue(result['can_continue'])
        for row in result['source_results'] + result['area_results']:
            self.assertEqual(row['message'].count('時間不足'), 1)
        serialized = encode(result | {'items': [job.model_dump(mode='json') for job in result['items']]})
        self.assertEqual(serialized.count('時間不足'), 6)  # Two sources, two areas, two cursor copies.
        state = json.loads(result['continuation'])['sources']['1111']
        self.assertEqual(state['areas']['all']['next_page'], 1)
        self.assertEqual(state['parser_warnings'], [])
        browser.call.return_value = page()
        resumed = await Sources(mcp_call=fake104, browser1111=browser).search(
            SearchInput(criteria={'keyword': '設計'}, sources=['104', '1111']), continuation=result['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 1)
        self.assertEqual(resumed['source_results'][1]['status'], 'success')
        self.assertNotIn('時間不足', encode(resumed | {'items': [job.model_dump(mode='json') for job in resumed['items']]}))

    async def test_worker_partial_budget_keeps_items_and_resumes_deferred_area_on_same_page(self):
        browser = AsyncMock()
        browser.call.side_effect = [dict(status='ok', areas=[
            dict(page(total=2), area_name='台南市'),
            dict(area_name='高雄市', status='time_limited', message='固定公開訊息')]),
            page([row(str(456 + i)) for i in range(10)], size=10, total=10), page([row('789')], number=2, total=2)]
        source = Sources(browser1111=browser)
        args = SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}, sources=['1111'], limit=5)
        first = await source.search(args)
        self.assertEqual([job.source_job_id for job in first['items']], ['123'])
        diag = first['source_results'][0]
        self.assertEqual(diag['status'], 'partial')
        self.assertIn('高雄市', diag['message'])
        self.assertFalse(diag['baseline_eligible'])
        self.assertEqual(diag['parser_warnings'], [])
        self.assertTrue(first['can_continue'])
        second = await source.search(args, continuation=first['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['area_names'], ['高雄市'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 1)
        self.assertEqual([job.source_job_id for job in second['items']], [str(456 + i) for i in range(10)])
        self.assertNotIn('時間不足', encode(second | {'items': [job.model_dump(mode='json') for job in second['items']]}))
        third = await source.search(args, continuation=second['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['area_names'], ['台南市'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 2)
        self.assertEqual([job.source_job_id for job in third['items']], ['789'])
        self.assertFalse(third['can_continue'])

    async def test_skipped_later_page_is_partial_and_resumes_that_page(self):
        browser = AsyncMock(); browser.call.return_value = page(total=2)
        source = Sources(browser1111=browser)
        args = SearchInput(criteria={'keyword': '品管'}, sources=['1111'], limit=1)
        first = await source.search(args)
        skipped = await source.search(args, continuation=first['continuation'], deadline=time.monotonic() + 1)
        self.assertEqual(skipped['items'], [])
        self.assertEqual(skipped['source_results'][0]['status'], 'partial')
        self.assertTrue(skipped['can_continue'])
        browser.call.return_value = page([row('456')], number=2, total=2)
        resumed = await source.search(args, continuation=skipped['continuation'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 2)
        self.assertNotIn('時間不足', encode(resumed | {'items': [job.model_dump(mode='json') for job in resumed['items']]}))

    async def test_skipped_only_source_is_failed_but_can_continue(self):
        browser = AsyncMock()
        result = await Sources(browser1111=browser).search(
            SearchInput(criteria={'keyword': '品管'}, sources=['1111']), deadline=time.monotonic() + 1)
        self.assertEqual(result['source_results'][0]['status'], 'failed')
        self.assertEqual(result['area_results'][0]['status'], 'not_queried')
        self.assertTrue(result['can_continue'])
        self.assertEqual(result['source_results'][0]['parser_warnings'], [])
        browser.call.assert_not_awaited()

    async def test_eleven_areas_are_explicitly_unsupported_before_browser(self):
        from app.sources import area_catalog
        names = area_catalog()[:11]
        browser = AsyncMock()
        result = await Sources(browser1111=browser).search(SearchInput(
            criteria={'keyword': '品管', 'area_codes': [item['code'] for item in names]}, sources=['1111']))
        self.assertEqual(result['source_results'][0]['status'], 'unsupported')
        self.assertEqual(result['source_results'][0]['message'], '1111 一次最多查 10 個地區，請縮小範圍。')
        browser.call.assert_not_awaited()
        self.assertFalse(result['can_continue'])

    async def test_detail_budget_skips_or_passes_only_time_before_reserve(self):
        from app.sources import SourceError
        browser = AsyncMock(); source = Sources(browser1111=browser)
        with patch('app.sources.time.monotonic', return_value=100):
            with self.assertRaises(SourceError) as error:
                await source.detail(job1111(), deadline=217.99, finish_reserve=45)
            self.assertEqual(error.exception.message, '時間不足，未讀取 1111 詳細內容。')
            browser.call.assert_not_awaited()
            browser.call.return_value = dict(status='ok', detail={})
            await source.detail(job1111(), deadline=218, finish_reserve=45)
        self.assertEqual(browser.call.call_args.kwargs['timeout'], SINGLE_AREA_SECONDS + STARTUP_SECONDS + WORKER_GRACE_SECONDS)

    async def test_detail_time_limited_status_and_message_match_admission_failure(self):
        from app.sources import SourceError
        browser = AsyncMock(); source = Sources(browser1111=browser)
        with patch('app.sources.time.monotonic', return_value=100):
            with self.assertRaises(SourceError) as admission:
                await source.detail(job1111(), deadline=172.99)
            browser.call.assert_not_awaited()
            browser.call.side_effect = BrowserError('time_limited', 'different public diagnostic')
            # Covers both lock expiry and post-lock admission: both carry this status.
            for deadline in (None, 340):
                with self.subTest(deadline=deadline):
                    with self.assertRaises(SourceError) as queued:
                        await source.detail(job1111(), deadline=deadline)
                    self.assertEqual(queued.exception.status, admission.exception.status)
                    self.assertEqual(queued.exception.status, 'failed')
                    self.assertEqual(queued.exception.message, admission.exception.message)
                    self.assertEqual(queued.exception.message, '時間不足，未讀取 1111 詳細內容。')
            self.assertEqual(browser.call.await_count, 2)
            browser.call.side_effect = BrowserError('blocked', 'public challenge diagnostic')
            with self.assertRaises(SourceError) as blocked:
                await source.detail(job1111())
            self.assertEqual(blocked.exception.status, 'blocked')
            self.assertEqual(blocked.exception.message, 'public challenge diagnostic')

    async def test_bridge_worker_budget_is_smaller_and_accounts_for_lock_wait(self):
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = 0; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        process.stdout.read.side_effect = [b'{"status":"ok"}', b'']
        elapsed = 100
        clock = Mock(); clock.monotonic.side_effect = lambda: elapsed
        held, release = asyncio.Event(), asyncio.Event()
        async def holder():
            async with bridge.lock:
                held.set()
                await release.wait()
        holding = asyncio.create_task(holder())
        await held.wait()
        with patch('app.source1111.time', clock), \
                patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process) as spawn:
            pending = asyncio.create_task(bridge.call({'action': 'search', 'area_names': ['甲', '乙']}, timeout=100))
            try:
                await asyncio.sleep(0)
                self.assertFalse(pending.done())
                spawn.assert_not_awaited()
                elapsed += 7
            finally:
                release.set()
                await holding
            await pending
            spawn.assert_awaited_once()
        payload = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(payload['budget_ms'], 85000)
        self.assertLess(payload['budget_ms'], 100000)

    async def test_search_passes_only_remaining_tool_budget_to_browser(self):
        browser = AsyncMock(); browser.call.return_value = page()
        await Sources(browser1111=browser).search(SearchInput(criteria={'keyword': '品管'}, sources=['1111']),
            deadline=time.monotonic() + 120, finish_reserve=45)
        self.assertGreater(browser.call.call_args.kwargs['timeout'], 73)
        self.assertLessEqual(browser.call.call_args.kwargs['timeout'], 75)

    async def test_bridge_timeout_scales_with_regions_and_honors_remaining_budget(self):
        bridge = Browser1111()
        real_timeout = asyncio.timeout
        for names, budget, expected in [(['甲', '乙', '丙'], None, 163), (['甲', '乙'], 57, 57), ([], None, 73), ([], 240, 240)]:
            with patch('app.source1111.asyncio.timeout', side_effect=real_timeout) as timeout, \
                    patch('app.source1111.time', Mock(monotonic=Mock(return_value=100))), \
                    patch('app.source1111.shutil.which', return_value=None):
                with self.assertRaises(BrowserError):
                    await bridge.call({'area_names': names}, timeout=budget)
                timeout.assert_called_once_with(expected)

    async def test_real_timeout_and_cancellation_cleanup_are_shielded(self):
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = None; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        async def stall(*args): await asyncio.sleep(10)
        process.stdout.read.side_effect = stall
        real_timeout = asyncio.timeout
        def short_timeout(seconds):
            timer = real_timeout(.01)
            reschedule = timer.reschedule
            timer.reschedule = lambda when: reschedule(min(when, asyncio.get_running_loop().time() + .01))
            return timer
        async def stop_process(owned):
            await asyncio.sleep(0)
            owned.returncode = -1
        with patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process), \
                patch('app.source1111.asyncio.timeout', side_effect=short_timeout), \
                patch.object(bridge, '_stop', side_effect=stop_process) as stop:
            with self.assertRaises(BrowserError) as error:
                await bridge.call({'area_names': ['甲', '乙']}, timeout=100)
            self.assertEqual(error.exception.status, 'failed')
            stop.assert_awaited_once_with(process)
            self.assertEqual(process.returncode, -1)

    async def test_timeout_during_startup_still_obtains_and_cleans_owned_process(self):
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = None
        real_timeout = asyncio.timeout
        def short_timeout(seconds):
            timer = real_timeout(.005)
            reschedule = timer.reschedule
            timer.reschedule = lambda when: reschedule(min(when, asyncio.get_running_loop().time() + .005))
            return timer
        async def launch(*args, **kwargs):
            await asyncio.sleep(.02)
            return process
        with patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', side_effect=launch), \
                patch('app.source1111.asyncio.timeout', side_effect=short_timeout), \
                patch.object(bridge, '_stop', new_callable=AsyncMock) as stop:
            with self.assertRaises(BrowserError):
                await bridge.call({'area_names': ['甲', '乙']}, timeout=100)
            stop.assert_awaited_once_with(process)
        self.assertFalse(bridge.lock.locked())

    async def test_search_rows_keep_optional_location_salary_and_dynamic_warning(self):
        # Removing the parser fields makes both values disappear and keeps the obsolete shared warning.
        payload = page([row('901') | {'location': '台南市善化區', 'salary': '月薪 35,000元~50,000元'}])
        result = search_rows(payload, 1)[0]
        self.assertEqual(result['location'], '台南市善化區')
        self.assertEqual(result['salary_text'], '月薪 35,000元~50,000元')
        self.assertEqual(result['filter_warnings'], ['1111 為實驗來源，可能因人機驗證受阻，請至來源核對。'])
        # A present field must never be reported as missing; the other field must be.
        for field, present, absent in [('location', '地點', '薪資'), ('salary', '薪資', '地點')]:
            missing = search_rows(page([row('902') | {field: 'x'}]), 1)[0]
            self.assertIn(absent + '未能解析', missing['filter_warnings'][0])
            self.assertNotIn(present, missing['filter_warnings'][0])

    async def test_search_rows_optional_fields_reject_invalid_values_without_page_failure(self):
        # Without per-field bounds/type checks, invalid optional metadata aborts or exceeds Job's 500-char limit.
        result = search_rows(page([row('903') | {'location': 42, 'salary': ' ' * 3}]), 1)[0]
        self.assertIsNone(result['location']); self.assertIsNone(result['salary_text'])
        result = search_rows(page([row('904') | {'location': 'z' * 501, 'salary': '月薪 50,000元以上'}]), 1)[0]
        self.assertIsNone(result['location']); self.assertEqual(result['salary_text'], '月薪 50,000元以上')

    async def test_full_page_overflow_and_continuation_preserve_unknowns(self):
        browser = AsyncMock()
        browser.call.side_effect = [page([row('123'), row('456')], size=2, total=3), page([row('789')], number=2, size=2, total=3)]
        source = Sources(browser1111=browser)
        args = SearchInput(criteria={'keyword': '品管', 'min_monthly_salary': 40000}, sources=['1111'], limit=1)
        first = await source.search(args)
        self.assertEqual(first['items'][0].source_job_id, '123')
        self.assertIsNone(first['items'][0].salary_text)
        self.assertIsNone(first['items'][0].location)
        self.assertNotIn('test@example', first['items'][0].summary)
        self.assertTrue(any('最低月薪' in w for w in first['items'][0].filter_warnings))
        second = await source.search(args, continuation=first['continuation'])
        self.assertEqual(second['items'][0].source_job_id, '456')
        self.assertEqual(browser.call.await_count, 1)
        third = await source.search(args, continuation=second['continuation'])
        self.assertEqual(third['items'][0].source_job_id, '789')
        self.assertFalse(third['can_continue'])
        self.assertEqual(browser.call.call_args.args[0]['page'], 2)

    async def test_blocked_does_not_erase_other_source_success(self):
        browser = AsyncMock(); browser.call.side_effect = BrowserError('blocked')
        result = await Sources(mcp_call=fake104, browser1111=browser).search(
            SearchInput(criteria={'keyword': '設計'}, sources=['104', '1111']))
        self.assertEqual([x.source for x in result['items']], ['104'])
        self.assertEqual(result['source_results'][1]['status'], 'blocked')
        self.assertFalse(result['source_results'][1]['baseline_eligible'])

    async def test_invalid_identity_and_incomplete_page_never_become_empty(self):
        for payload in [page([], total=4), page([row('321') | {'url': 'https://evil.invalid/job/321'}]),
                        page([row('456') | {'url': 'https://www.1111.com.tw/job/123'}]),
                        page([row('444') | {'companyUrl': 'https://evil.invalid/corp/987'}])]:
            with self.subTest(payload=payload):
                with self.assertRaises(BrowserError): search_rows(payload, 1)

    async def test_page_five_stops_with_explicit_limit(self):
        browser = AsyncMock(); browser.call.side_effect = lambda args: page([row(str(args['page']))], number=args['page'], total=6)
        result = await Sources(browser1111=browser).search(SearchInput(criteria={'keyword': '製圖'}, sources=['1111']))
        self.assertEqual(browser.call.await_count, 5)
        self.assertFalse(result['can_continue'])
        self.assertTrue(any('五頁' in w for w in result['source_results'][0]['parser_warnings']))
        self.assertTrue(result['source_results'][0]['truncated'])
        self.assertFalse(result['source_results'][0]['baseline_eligible'])

    async def test_detail_verifies_numeric_job_and_company_identity(self):
        browser = AsyncMock()
        detail = dict(id='123', url='https://www.1111.com.tw/job/123', title='品管工程師', company='合成製造公司',
                      companyUrls=['https://www.1111.com.tw/corp/987'], description='工作內容 test@example.invalid')
        browser.call.return_value = dict(status='ok', detail=detail)
        source = Sources(browser1111=browser)
        verified = await source.detail(job1111())
        self.assertTrue(verified['identity_verified'])
        self.assertNotIn('test@example', verified['detail']['summary'])
        for changes in [dict(id='456'), dict(companyUrls=['https://www.1111.com.tw/corp/321']), dict(title='另一個職缺')]:
            browser.call.return_value = dict(status='ok', detail=detail | changes)
            self.assertFalse((await source.detail(job1111()))['identity_verified'])

    async def test_missing_node_and_cancel_do_not_report_empty_or_swallow_cancel(self):
        bridge = Browser1111()
        with patch('app.source1111.shutil.which', return_value=None):
            with self.assertRaises(BrowserError) as error: await bridge.call({'action': 'search'})
            self.assertEqual(error.exception.status, 'unsupported')
        process = AsyncMock(); process.returncode = None; process.stdin = Mock()
        process.stdin.drain = AsyncMock(); process.stdout.read = AsyncMock(side_effect=asyncio.CancelledError)
        with patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process), \
                patch.object(bridge, '_stop', new_callable=AsyncMock) as stop:
            with self.assertRaises(asyncio.CancelledError): await bridge.call({'action': 'search'})
            stop.assert_awaited_once_with(process)

    async def test_windows_cleanup_only_targets_owned_subtree(self):
        bridge = Browser1111(); process = AsyncMock(); process.pid = 7654321
        killer = AsyncMock()
        with patch('app.source1111.os.name', 'nt'), patch('app.source1111.asyncio.create_subprocess_exec', return_value=killer) as execute:
            await bridge._stop(process)
        self.assertEqual(execute.call_args.args, ('taskkill', '/PID', '7654321', '/T', '/F'))
        process.wait.assert_awaited_once()

    async def test_daily_runner_obeys_both_switches_and_records_blocked(self):
        from app.scheduler import DailyRunner
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect(write=True) as db:
                store.put(db, 'default_preferences', [{'keyword': '品管'}])
                store.put(db, 'services', {'source_518_enabled': False, 'source_1111_enabled': False})
            browser = AsyncMock(); browser.call.side_effect = BrowserError('blocked')
            runner = DailyRunner(store, Sources(mcp_call=fake104, browser1111=browser), automatic=False)
            runner.start('manual'); await runner.task
            browser.call.assert_not_awaited()
            with store.connect(write=True) as db:
                store.put(db, 'services', {'source_518_enabled': False, 'source_1111_enabled': True})
            ident = runner.start('manual'); await runner.task
            browser.call.assert_awaited_once()
            with store.connect() as db:
                result = json.loads(db.execute('SELECT result_json FROM runs WHERE id=?', (ident,)).fetchone()[0])
                self.assertEqual([x['source'] for x in result['source_results']], ['104', '1111'])
                self.assertEqual(result['source_results'][1]['status'], 'blocked')
                self.assertFalse(result['source_results'][1]['completed'])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM preference_baselines WHERE source='1111'").fetchone()[0], 0)

    async def test_daily_multi_area_budget_never_establishes_partial_baseline(self):
        from app.scheduler import DailyRunner, daily_1111_seconds
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect(write=True) as db:
                store.put(db, 'default_preferences', [{'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}])
                store.put(db, 'services', {'source_518_enabled': False, 'source_1111_enabled': True})
            browser = AsyncMock(); browser.call.return_value = dict(status='ok', areas=[
                dict(page(), area_name='台南市'), dict(area_name='高雄市', status='time_limited', message='時間不足')])
            source = Sources(mcp_call=fake104, browser1111=browser)
            with patch.object(source, 'search', wraps=source.search) as search, \
                    patch('app.scheduler.time.monotonic', return_value=100):
                runner = DailyRunner(store, source, automatic=False)
                ident = runner.start('manual'); await runner.task
            self.assertEqual(search.call_args.kwargs['deadline'], 100 + daily_1111_seconds(SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}).criteria))
            self.assertLessEqual(browser.call.call_args.kwargs['timeout'], daily_1111_seconds(SearchInput(criteria={'keyword': '品管', 'area_codes': ['6001014000', '6001016000']}).criteria))
            with store.connect() as db:
                result = json.loads(db.execute('SELECT result_json FROM runs WHERE id=?', (ident,)).fetchone()[0])
                diag = next(item for item in result['source_results'] if item['source'] == '1111')
                self.assertEqual(diag['status'], 'partial')
                self.assertIn('高雄市', diag['message'])
                self.assertFalse(diag['completed'])
                self.assertFalse(diag['baseline'])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM preference_baselines WHERE source='1111'").fetchone()[0], 0)

    async def test_bridge_reads_segmented_json_until_eof(self):
        bridge = Browser1111()
        output = json.dumps({'status': 'ok', 'detail': {'description': 'x' * 100000}}).encode()
        process = AsyncMock(); process.returncode = 0; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        process.stdout.read.side_effect = [output[:500], output[500:60000], output[60000:], b'']
        with patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process):
            result = await bridge.call({'action': 'detail'})
        self.assertEqual(len(result['detail']['description']), 100000)
        self.assertEqual(process.stdout.read.await_count, 4)

    async def test_timeout_cleans_owned_process_and_next_call_can_run(self):
        bridge = Browser1111()
        process = AsyncMock(); process.returncode = None; process.stdin = Mock(); process.stdin.drain = AsyncMock()
        process.stdout.read = AsyncMock(side_effect=TimeoutError)
        with patch('app.source1111.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('app.source1111.asyncio.create_subprocess_exec', return_value=process), \
                patch.object(bridge, '_stop', new_callable=AsyncMock) as stop:
            with self.assertRaises(BrowserError) as error: await bridge.call({'action': 'search'})
            self.assertEqual(error.exception.status, 'failed')
            self.assertIn('執行時間', error.exception.message)
            self.assertIn('已停止本次瀏覽器', error.exception.message)
            stop.assert_awaited_once()
        self.assertFalse(bridge.lock.locked())


class RouteChecks(unittest.TestCase):
    setUp = ChatChecks.setUp
    tearDown = ChatChecks.tearDown

    def configure(self, enabled=False, explicit=None):
        with self.store.connect(write=True) as db: self.store.put(db, 'services', SETTINGS | {'source_518_enabled': False, 'source_1111_enabled': enabled})
        self.browser = AsyncMock(); self.browser.call.return_value = page()
        self.app.state.sources = Sources(mcp_call=fake104, browser1111=self.browser)
        count = 0
        def provider(request):
            nonlocal count
            count += 1
            arguments = {'criteria': {'keyword': '品管'}}
            if explicit is not None: arguments['sources'] = explicit
            return response(call=('search1', 'search_jobs', encode(arguments))) if count == 1 else response(text='已整理')
        self.app.state.llm_transport = __import__('httpx').MockTransport(provider)

    def send(self, ident, text='合成來源查詢'):
        return events(self.client.post('/api/chat', json={'request_id': ident, 'text': text}))

    def test_chat_deadline_reaches_search_and_skips_1111_with_a_completed_answer(self):
        from app import chat, sources
        self.configure(True)
        self.app.state.sources.mcp_call = AsyncMock(wraps=fake104)
        search_budget = (sources._HTTP_TIMEOUT_SECONDS + SINGLE_AREA_SECONDS + STARTUP_SECONDS + WORKER_GRACE_SECONDS) / 2
        with patch.object(chat, 'CHAT_SECONDS', search_budget + .1), patch.object(chat, 'FINISH_RESERVE_SECONDS', .1), \
                patch.object(chat, 'TOOL_MODEL_ALLOWANCE_SECONDS', 0):
            output = self.send('short-search')
        self.app.state.sources.mcp_call.assert_awaited_once()
        self.browser.call.assert_not_awaited()
        jobs = next(data for event, data in output if event == 'jobs')
        self.assertEqual({item['source'] for item in jobs['items']}, {'104'})
        diagnostic = next(item for item in jobs['source_results'] if item['source'] == '1111')
        self.assertEqual(diagnostic['status'], 'partial')
        self.assertIn('時間不足', diagnostic['message'])
        self.assertNotIn(diagnostic['message'], jobs['filter_warnings'])
        self.assertTrue(jobs['can_continue'])
        self.assertEqual(diagnostic['parser_warnings'], [])
        self.assertEqual(output[-1][1]['status'], 'completed')
        self.assertIn('已整理', ''.join(data['text'] for event, data in output if event == 'text_delta'))

    def test_default_off_zero_browser_calls_and_on_implicit_search(self):
        self.configure()
        off = next(x for event, x in self.send('off') if event == 'jobs')
        self.assertEqual(off['sources'], ['104'])
        self.assertFalse(any('1111' in w for w in off['filter_warnings']))
        self.browser.call.assert_not_awaited()
        self.configure(True)
        on = next(x for event, x in self.send('on') if event == 'jobs')
        self.assertEqual(on['sources'], ['104', '1111'])
        self.assertEqual({x['source'] for x in on['items']}, {'104', '1111'})
        self.browser.call.assert_awaited_once()

    def test_enabled_1111_is_queried_unless_the_user_names_a_source(self):
        # The model narrowing sources on its own was how 1111 got skipped although the admin had turned it on.
        self.configure(True, ['104'])
        output = self.send('narrowed')
        narrowed = next(x for event, x in output if event == 'jobs')
        self.assertEqual(narrowed['sources'], ['104', '1111'])
        self.browser.call.assert_awaited_once()
        progress = [x for event, x in output if event == 'tool_status' and x.get('name') == 'search_jobs' and x.get('phase') == 'started']
        self.assertIn('104／1111', progress[0]['summary'], 'the progress label names the sources actually queried')
        for index, text in enumerate(('只查 104 就好', '這次不要1111', '只查１０４', '只查 104.',
                                      '只查 https://www.104.com.tw 的職缺', '只查1111.com.tw',
                                      '104人力銀行', '用104跟518找', '1111 和 518')):
            with self.subTest(text=text):
                self.configure(True, ['104'])
                chosen = next(x for event, x in self.send(f'chosen-{index}', text) if event == 'jobs')
                self.assertEqual(chosen['sources'], ['104'])
                self.browser.call.assert_not_awaited()
        # Numbers that are addresses, wages or codes do not count as naming a source.
        for index, text in enumerate(('中山區104號附近', '時薪518元以上', '代號A1111B',
                                      '51040', '月薪 104 千', '$104', '104.5', 'x104.com.tw', '104.com.tw.evil.invalid')):
            with self.subTest(text=text):
                with self.store.connect(write=True) as db: db.execute('DELETE FROM messages')
                self.configure(True, ['104'])
                kept = next(x for event, x in self.send(f'number-{index}', text) if event == 'jobs')
                self.assertEqual(kept['sources'], ['104', '1111'])

    def test_preference_survives_followups_and_warning_continuation(self):
        self.configure(True, ['1111'])
        self.browser.call.return_value = page([row(str(i)) for i in range(1, 31)], size=30, total=31)
        first = next(data for event, data in self.send('preference', '只查1111') if event == 'jobs')
        warning = '依你的指定，本次未查 104。'
        self.assertIn(warning, first['filter_warnings'])
        self.configure(True, ['1111'])
        output = self.send('followup', '再找產品經理')
        jobs = next(data for event, data in output if event == 'jobs')
        self.assertEqual(jobs['sources'], ['1111'])
        self.assertIn(warning, jobs['filter_warnings'])
        progress = [data for event, data in output if event == 'tool_status'
                    and data['name'] == 'search_jobs' and data['phase'] == 'started']
        self.assertEqual(len(progress), 1)
        label = progress[0]['summary']
        self.assertTrue(label.endswith('· 1111'), label)
        self.app.state.llm_transport = __import__('httpx').MockTransport(lambda request: response(text='續查完成'))
        output = events(self.client.post('/api/chat', json={'request_id': 'continued', 'text': '繼續',
            'action': {'type': 'continue_search', 'search_id': first['search_id']}}))
        jobs = next(data for event, data in output if event == 'jobs')
        self.assertEqual(jobs['filter_warnings'].count(warning), 1)
        self.assertEqual(jobs['sources'], ['1111'])

    def test_model_continuation_label_uses_the_saved_sources(self):
        self.configure(True, ['1111'])
        self.browser.call.return_value = page([row(str(i)) for i in range(1, 31)], size=30, total=31)
        first = next(x for event, x in self.send('first', '只查1111') if event == 'jobs')
        calls = iter([response(call=('next1', 'search_jobs', encode({'criteria': {'keyword': '品管'}, 'search_id': first['search_id']}))),
                      response(text='已續查')])
        self.app.state.llm_transport = __import__('httpx').MockTransport(lambda request: next(calls))
        output = self.send('model-next', '再多給一些')
        label = next(x['summary'] for event, x in output if event == 'tool_status' and x.get('name') == 'search_jobs' and x.get('phase') == 'started')
        self.assertTrue(label.endswith('· 1111'), label)
        self.assertEqual(next(x for event, x in output if event == 'jobs')['sources'], ['1111'])

    def test_explicit_disabled_1111_names_correct_source_and_detail_off(self):
        self.configure(explicit=['1111'])
        output = self.send('disabled', '只查1111')
        error = next(x for event, x in output if event == 'error')
        self.assertEqual(error['code'], 'source_disabled')
        self.assertIn('1111', error['message'])
        self.assertNotIn('518', error['message'])
        ident = self.store.save_job(job1111())
        result = self.client.post('/api/jobs/' + ident + '/details').json()
        self.assertEqual(result['status'], 'unsupported')
        self.browser.call.assert_not_awaited()

    def test_settings_false_default_and_saved_switch_survive_restart(self):
        self.assertFalse(self.client.get('/api/admin/settings').json()['source_1111_enabled'])
        for enabled in (True, False):
            reply = self.client.put('/api/admin/settings', json={'source_1111_enabled': enabled},
                                   headers={'X-CSRF-Token': security.csrf(self.admin_token)})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(self.client.get('/api/admin/settings').json()['source_1111_enabled'], enabled)
            self.assertEqual(Store(self.directory.name).path, self.store.path)

    def test_continue_after_1111_disabled_never_calls_browser_again(self):
        self.configure(True, ['1111'])
        self.browser.call.return_value = page([row(str(i)) for i in range(1, 31)], size=30, total=31)
        first = next(x for event, x in self.send('first', '只查1111') if event == 'jobs')
        self.assertTrue(first['can_continue'])
        self.client.put('/api/admin/settings', json={'source_1111_enabled': False},
                        headers={'X-CSRF-Token': security.csrf(self.admin_token)})
        output = events(self.client.post('/api/chat', json={'request_id': 'next', 'text': '繼續找',
            'action': {'type': 'continue_search', 'search_id': first['search_id']}}))
        self.assertTrue(any(event == 'error' and x.get('code') == 'source_disabled' for event, x in output))
        self.browser.call.assert_awaited_once()


class DurableChecks(unittest.TestCase):
    def test_schema6_upgrade_preserves_favorites_and_new1111_daily_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Store(directory, target_version=6)
            original = Job(source='104', source_job_id='old', title='舊職缺', company_name='合成公司', url='https://www.104.com.tw/job/old')
            ident = old.save_job(original)
            with old.connect(write=True) as db:
                db.execute('INSERT INTO favorites VALUES(?,?,?)', (ident, 'default', now().isoformat()))
            store = Store(directory)
            with store.connect(write=True) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
                self.assertEqual(db.execute('SELECT job_id FROM favorites').fetchone()[0], ident)
                result = commit_results(store, db, 'synthetic', '2026-10-11', [dict(preference_id='p', revision=1, source='1111',
                    user_id='default', items=[job1111()], eligible=True, diagnostic={'status': 'success'})], now().isoformat())
                self.assertEqual(result['initial_count'], 1)
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_schema7_failure_rolls_back_rebuilt_jobs_and_version(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            old = Store(directory, target_version=6)
            ident = old.save_job(Job(source='104', source_job_id='old', title='舊職缺', company_name='合成公司', url='https://www.104.com.tw/job/old'))
            with old.connect(write=True) as db:
                db.execute('INSERT INTO favorites VALUES(?,?,?)', (ident, 'default', now().isoformat()))
                before = backups.schema_catalog(db)
                # Force failure after jobs has already been copied, dropped and renamed.
                db.execute('CREATE TABLE preference_baselines_v7(blocked TEXT)')
            with self.assertRaises(sqlite3.OperationalError): Store(directory)
            with old.connect(write=True) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
                self.assertEqual(db.execute('SELECT job_id FROM favorites').fetchone()[0], ident)
                self.assertEqual(db.execute('SELECT id FROM jobs').fetchone()[0], ident)
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
                db.execute('DROP TABLE preference_baselines_v7')
                self.assertEqual(backups.schema_catalog(db), before)

    def test_backup_versions4_to7_keep_canonical_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); catalogs = {}
            for version in (4, 5, 6, 7):
                store = Store(root / str(version), target_version=version)
                if version == 7: store.save_job(job1111())
                with store.connect() as db: catalogs[version] = backups.schema_catalog(db)
                backup = backups.create_backup(store)
                stage = root / ('stage' + str(version)); stage.mkdir()
                result = validate_backup(store.path.parent / 'backups' / (backup['id'] + '.zip'), stage, {version: catalogs[version]})
                self.assertEqual(result['schema_version'], version)
            self.assertNotEqual(catalogs[6], catalogs[7])

    def test_enabled_sources_defaults_and_daily_registry(self):
        self.assertEqual(enabled_sources({}), ['104', '518'])
        self.assertEqual(enabled_sources({'source_1111_enabled': True, 'source_518_enabled': False}), ['104', '1111'])


del ChatChecks
if __name__ == '__main__': unittest.main()
