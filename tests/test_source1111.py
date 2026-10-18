"""Offline 1111 contract, switches, continuation and durable compatibility."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch

from app import backups, security
from app.backup_verify import validate_backup
from app.daily import commit_results
from app.source1111 import Browser1111, BrowserError, search_rows
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
            self.assertIn('等候時間', error.exception.message)
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
                self.configure(True, ['104'])
                kept = next(x for event, x in self.send(f'number-{index}', text) if event == 'jobs')
                self.assertEqual(kept['sources'], ['104', '1111'])

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
