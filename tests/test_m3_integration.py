"""Offline production-route checks with temporary data and injected sources."""
import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import security
from app.main import create_app
from app.scheduler import DailyRunner
from app.sources import DailySearchInput, SearchInput, Sources
from app.store import Job, Store, encode, now

UTC = timezone.utc
CRITERIA = {'keyword': '合成離線條件', 'area_codes': [], 'min_monthly_salary': None}


class OfflineSources:
    def __init__(self):
        self.items = ['old']
        self.fail = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.calls = 0

    async def search(self, arguments):
        self.calls += 1
        self.started.set()
        while not self.release.is_set(): await asyncio.sleep(.005)
        source = arguments.sources[0]
        return {'items': [] if self.fail else [Job(source=source, source_job_id=i, title='合成職缺',
                company_name='合成公司', url='https://example.invalid/'+i) for i in self.items],
                'source_results': [{'source': source, 'status': 'failed' if self.fail else 'success',
                    'baseline_eligible': not self.fail, 'truncated': False, 'message': None}], 'filter_warnings': []}


class Routes(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, automatic_daily=False)
        self.store = self.app.state.store
        self.fake = OfflineSources()
        self.app.state.daily.sources = self.fake
        self.user, self.admin = 'u'*43, 'a'*43
        with self.store.connect(write=True) as db:
            for role, token in [('user', self.user), ('admin', self.admin)]:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), role,
                           (now()+timedelta(days=1)).isoformat(), 'synthetic-'+role, 'default' if role == 'user' else None))
            db.execute("UPDATE users SET has_chosen=1 WHERE id='default'")
            db.execute('INSERT INTO preferences VALUES(?,1,?,1,?)', ('p', encode(CRITERIA), 'default'))
        self.client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1))
        self.client.__enter__()
        self.client.cookies.set('jc_admin', self.admin)
        self.client.cookies.set('jc_user', self.user)
        self.client.headers.update({'Origin': 'http://localhost', 'X-CSRF-Token': security.csrf(self.admin)})

    def tearDown(self):
        self.fake.release.set()
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def finish(self, ident):
        for _ in range(300):
            with self.store.connect() as db:
                row = db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
                if row['status'] != 'running': return row
            time.sleep(.005)
        self.fail('Offline daily task did not finish')

    def run_daily(self):
        response = self.client.post('/api/admin/search-runs')
        self.assertEqual(response.status_code, 202, response.text)
        return self.finish(response.json()['id'])

    def test_real_routes_baseline_new_count_pagination_read_favorite_and_privacy(self):
        first = self.run_daily()
        self.assertEqual(json.loads(first['result_json'])['new_count'], 0)
        self.fake.items = ['old', 'new']
        second = self.run_daily()
        self.assertEqual(json.loads(second['result_json'])['new_count'], 2)
        self.assertEqual(json.loads(self.run_daily()['result_json'])['new_count'], 0)
        first_page = self.client.get('/api/daily-jobs?limit=1').json()
        self.assertEqual(len(first_page['items']), 1)
        job = first_page['items'][0]
        self.assertTrue(job['is_initial'])
        previous_date = (datetime.fromisoformat(first_page['local_date'])-timedelta(days=1)).date().isoformat()
        with self.store.connect(write=True) as db:
            db.execute('INSERT INTO daily_jobs VALUES(?,?,?,?,1,NULL)', ('default', previous_date, job['id'], first['id']))
            chat_only = self.store.save_job(Job(source='104', source_job_id='chat-only', title='合成聊天職缺',
                                      company_name='合成公司', url='https://example.invalid/chat-only'), db)
        self.client.headers['X-CSRF-Token'] = security.csrf(self.user)
        self.assertEqual(self.client.post('/api/jobs/'+job['id']+'/read').status_code, 200)
        self.assertEqual(self.client.put('/api/favorites/'+job['id']).status_code, 200)
        again = self.client.get('/api/daily-jobs?limit=1').json()['items'][0]
        self.assertTrue(again['read_at'])
        self.assertTrue(again['is_favorite'])
        self.assertEqual(self.client.post('/api/jobs/'+job['id']+'/read').status_code, 200)
        self.assertEqual(self.client.get('/api/daily-jobs?limit=1').json()['items'][0]['read_at'], again['read_at'])
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT read_at FROM daily_jobs WHERE local_date=? AND job_id=?',
                              (previous_date, job['id'])).fetchone()[0])
        self.assertEqual(self.client.post('/api/jobs/'+job['id']+'/read', params={'date': previous_date}).status_code, 200)
        self.assertEqual(self.client.post('/api/jobs/'+chat_only+'/read').status_code, 404)
        self.assertEqual(self.client.post('/api/jobs/'+job['id']+'/read?date=1999-01-01').status_code, 422)
        last = self.client.get('/api/daily-jobs', params={'cursor': first_page['next_cursor']}).json()
        self.assertEqual(len(last['items']), 3)
        bootstrap = self.client.get('/api/bootstrap')
        for forbidden in ['endpoint', 'keys_json', 'password_hash', 'api_key', self.admin, self.user]:
            self.assertNotIn(forbidden, bootstrap.text)
        self.assertEqual(self.client.get('/api/daily-jobs?date=1999-01-01').status_code, 422)
        self.assertEqual(self.client.get('/api/daily-jobs?date=2026-1-01').status_code, 422)

    def test_busy_admission_and_admin_revoked_after_await_has_zero_submission(self):
        self.fake.release.clear()
        response = self.client.post('/api/admin/search-runs')
        ident = response.json()['id']
        self.assertTrue(self.fake.started.wait(1))
        self.assertEqual(self.client.post('/api/admin/search-runs').status_code, 409)
        self.assertEqual(self.client.post('/api/admin/logout').status_code, 204)
        self.fake.release.set()
        self.assertEqual(self.finish(ident)['status'], 'failed')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM seen_jobs').fetchone()[0], 0)
        with TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1)) as anonymous:
            self.assertEqual(anonymous.get('/api/daily-jobs').status_code, 401)

    def test_changed_conditions_after_await_cannot_submit_old_baseline(self):
        self.fake.release.clear()
        response = self.client.post('/api/admin/search-runs')
        ident = response.json()['id']
        self.assertTrue(self.fake.started.wait(1))
        with self.store.connect(write=True) as db:
            db.execute('UPDATE preferences SET revision=2 WHERE id=?', ('p',))
        self.fake.release.set()
        self.assertEqual(self.finish(ident)['status'], 'failed')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM preference_baselines').fetchone()[0], 0)
        self.assertEqual(json.loads(self.run_daily()['result_json'])['new_count'], 0)

    def test_schedule_and_default_revisions_pause_without_reinserting_defaults(self):
        for value in ['24:00','09:60','9:00','00:000']:
            self.assertEqual(self.client.put('/api/admin/schedule', json={'time': value}).status_code, 422)
        self.assertEqual(self.client.put('/api/admin/schedule', json={'time': '23:59'}).status_code, 200)
        self.assertEqual(self.client.get('/api/admin/schedule').json()['timezone'], 'Asia/Taipei')
        for _ in range(2): self.client.put('/api/admin/default-preferences', json={'preferences': [CRITERIA]})
        with self.store.connect(write=True) as db:
            self.assertEqual(self.store.get(db, 'default_preferences_revision'), 1)
            db.execute('DELETE FROM preferences')
        self.fake.calls = 0
        result = json.loads(self.run_daily()['result_json'])
        self.assertTrue(result['paused'])
        self.assertEqual(self.fake.calls, 0)

    def test_uploaded_png_private_preview_bad_file_and_disk_failure_keep_previous(self):
        payload = io.BytesIO()
        Image.new('RGBA', (8, 4), (10, 20, 30, 255)).save(payload, format='PNG')
        data = payload.getvalue()
        fields = {'frame_width': '4', 'frame_height': '4', 'frame_count': '2', 'fps': '8'}
        route = '/api/admin/sprites/idle'
        saved = self.client.put(route, data=fields, files={'file': ('test.png', data, 'image/png')})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn('path', self.client.get('/api/admin/sprites').text)
        self.assertEqual(self.client.get(route+'/image').content, data)
        self.assertEqual(self.client.put(route, data=fields, files={'file': ('broken.png', b'bad', 'image/png')}).status_code, 422)
        with patch.object(Path, 'write_bytes', side_effect=OSError('synthetic disk full')):
            response = self.client.put(route, data=fields, files={'file': ('new.png', data, 'image/png')})
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get(route+'/image').content, data)
        with TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1)) as anonymous:
            self.assertEqual(anonymous.get('/api/sprites/idle/image').status_code, 401)
        self.assertEqual(self.client.get('/static/../data/sprites/test.png').status_code, 404)
        self.assertEqual(self.client.put('/api/admin/quotes', json={'texts': []}).json(), {'texts': []})
        self.assertEqual(self.client.put('/api/admin/quotes', json={'texts': ['<script>text only</script>']}).status_code, 200)

    def test_template_preview_atomic_import_and_failures_keep_all_previous(self):
        image = Image.new('RGBA', (512, 320))
        for row in range(5): image.paste((row * 45, 30, 90, 255), (1, row * 64 + 1, 511, (row + 1) * 64 - 1))
        payload = io.BytesIO(); image.save(payload, format='PNG'); image.close()
        files = {'file': ('template.png', payload.getvalue(), 'image/png')}
        route = '/api/admin/sprite-template'
        preview = self.client.post(route+'/preview', files=files)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual([item['state'] for item in preview.json()['items']], ['idle','thinking','happy','mail','encourage'])
        self.assertTrue(all((item['frame_count'], item['fps']) == (8, 8) for item in preview.json()['items']))
        self.assertEqual(self.client.get('/api/admin/sprites').json()['items'], [])
        self.assertEqual(self.client.put(route, files=files).status_code, 422)
        saved = self.client.put(route, files=files, data={'confirm': 'true', 'fps': '12'})
        self.assertEqual(saved.status_code, 200, saved.text)
        for row, item in enumerate(saved.json()['items']):
            self.assertEqual((item['frame_width'], item['frame_height'], item['frame_count'], item['fps']), (64,64,8,12))
            with Image.open(io.BytesIO(self.client.get('/api/admin/sprites/'+item['state']+'/image').content)) as strip:
                self.assertEqual(strip.size, (512, 64))
                self.assertEqual(strip.getpixel((32,32)), (row * 45,30,90,255))
                self.assertEqual(strip.getpixel((0,0))[3], 0)
        directory = self.store.path.parent / 'sprites'
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        rows = self.client.get('/api/admin/sprites').json()
        self.assertEqual(self.client.put(route, files={'file': ('bad.png', b'bad', 'image/png')}, data={'confirm':'true'}).status_code, 422)
        write_bytes = Path.write_bytes
        calls = 0
        def partial_disk_failure(path, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                write_bytes(path, b'partial')
                raise OSError('synthetic disk full')
            return write_bytes(path, data)
        with patch.object(Path, 'write_bytes', partial_disk_failure):
            self.assertEqual(self.client.put(route, files=files, data={'confirm':'true'}).status_code, 503)
        with self.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER fail_template BEFORE UPDATE ON sprites WHEN NEW.state='happy' BEGIN SELECT RAISE(ABORT,'synthetic DB failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.put(route, files=files, data={'confirm':'true'})
        self.assertEqual(self.client.get('/api/admin/sprites').json(), rows)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
        with TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1)) as anonymous:
            anonymous.headers['Origin'] = 'http://localhost'
            self.assertEqual(anonymous.post(route+'/preview', files=files).status_code, 401)
            self.assertEqual(anonymous.put(route, files=files, data={'confirm':'true'}).status_code, 401)
        self.client.headers['X-CSRF-Token'] = 'invalid'
        self.assertEqual(self.client.put(route, files=files, data={'confirm':'true'}).status_code, 403)

    def test_more_template_frames_and_opt_in_cleanup_match_preview_and_saved(self):
        route = '/api/admin/sprite-template'
        for columns in (4, 8, 12):
            with self.subTest(columns=columns):
                image = Image.new('RGBA', (columns * 64, 320))
                image.paste('black', (20,63,28,64))
                image.paste('black', (22,64,26,66))
                image.paste('red', (18,80,38,105))
                output = io.BytesIO(); image.save(output, format='PNG'); image.close()
                files = {'file': ('template.png', output.getvalue(), 'image/png')}
                fields = {'columns': str(columns), 'fps': '4', 'clean_edges': 'true'}
                before = self.client.get('/api/admin/sprites').json()
                preview = self.client.post(route+'/preview', files=files, data=fields)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(self.client.get('/api/admin/sprites').json(), before)
                for item in preview.json()['items']:
                    self.assertEqual((item['frame_count'],item['fps']), (columns,4))
                saved = self.client.put(route, files=files, data={**fields, 'confirm':'true'})
                self.assertEqual(saved.status_code, 200, saved.text)
                for item in saved.json()['items']:
                    self.assertEqual((item['frame_count'],item['fps']), (columns,4))
                with Image.open(io.BytesIO(self.client.get('/api/admin/sprites/thinking/image').content)) as strip:
                    self.assertEqual(strip.size, (columns*64,64))
                    # Spill removed (else the bbox would reach row 0) and the body centred on the ground line.
                    self.assertEqual(strip.crop((0,0,64,64)).getchannel('A').getbbox(), (22,35,42,60))
                    self.assertEqual(strip.getpixel((30,45)), (255,0,0,255))
                prior = self.client.get('/api/admin/sprites').json()
                wrong_columns = '8' if columns == 4 else '4'
                self.assertEqual(self.client.put(route, files=files, data={**fields,'columns':wrong_columns,'confirm':'true'}).status_code, 422)
                self.assertEqual(self.client.get('/api/admin/sprites').json(), prior)

    def test_service_worker_root_scope_manifest_and_public_offline_shell(self):
        response = self.client.get('/sw.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['service-worker-allowed'], '/')
        self.assertIn('javascript', response.headers['content-type'])
        manifest = self.client.get('/static/manifest.webmanifest').json()
        self.assertEqual(manifest['scope'], '/')
        shell = self.client.get('/static/offline.html')
        self.assertEqual(shell.status_code, 200)
        self.assertNotIn('csrf_token', shell.text)


class SchedulerChecks(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_taiwan_clock_restart_failure_and_manual_before_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect(write=True) as db:
                store.put(db, 'default_preferences', [CRITERIA])
            fake = OfflineSources()
            fake.fail = True
            runner = DailyRunner(store, fake)
            clock = datetime(2026, 10, 5, 0, 59, tzinfo=UTC)  # Taiwan 08:59
            runner.clock = lambda: clock
            ident = runner.start('manual')
            await runner.task
            self.assertEqual(fake.calls, 2)
            await runner.tick(catchup=True)
            self.assertIsNone(runner.task)  # manual before 09:00 is not repeated baseline polling
            clock += timedelta(minutes=2)
            await runner.tick(catchup=True)
            self.assertIsNotNone(runner.task)
            await runner.task
            self.assertEqual(fake.calls, 4)
            restarted = DailyRunner(store, fake)
            restarted.clock = lambda: clock
            restarted.recover()
            await restarted.tick(catchup=True)
            self.assertIsNone(restarted.task)
            self.assertEqual(fake.calls, 4)  # all-failed day does not create an infinite baseline retry
            with store.connect() as db:
                triggers = [r[0] for r in db.execute('SELECT trigger FROM runs ORDER BY rowid')]
            self.assertEqual(triggers, ['manual', 'catchup'])

    async def test_interrupted_run_counts_and_only_today_is_caught_up(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            fixed = datetime(2026, 10, 6, 2, tzinfo=UTC)
            with store.connect(write=True) as db:
                store.put(db, 'default_preferences', [CRITERIA])
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,NULL)', ('stale', '2026-10-05', 'scheduled', 'running', encode({'new_count': 0, 'initial_count': 0, 'source_results': []}), '2026-10-05T01:00:00+00:00'))
            fake = OfflineSources()
            runner = DailyRunner(store, fake)
            runner.clock = lambda: fixed
            runner.recover()
            await runner.tick(catchup=True)
            await runner.task
            with store.connect() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY rowid')]
            self.assertEqual([r['local_date'] for r in rows], ['2026-10-05','2026-10-06'])
            self.assertEqual(rows[0]['status'], 'failed')
            self.assertTrue(json.loads(rows[0]['result_json'])['interrupted'])
            await runner.tick(catchup=True)
            self.assertIsNone(runner.task)


class SourceLimits(unittest.IsolatedAsyncioTestCase):
    async def test_daily_reuses_real_source_loop_100_and_five_pages_chat_stays_20(self):
        pages = []
        async def mcp(name, args):
            pages.append(args['page'])
            self.assertEqual(args['page_size'], 20)
            rows = [{'job_no': str((args['page']-1)*20+i), 'job_name': '職缺', 'company_name': '公司',
                     'url': 'https://www.104.com.tw/job/j'+str((args['page']-1)*20+i)} for i in range(20)]
            return {'jobs': rows, 'total': 1000, 'has_next': True}, None
        source = Sources(mcp_call=mcp)
        result = await source.search(DailySearchInput(criteria=CRITERIA, sources=['104']))
        self.assertEqual(pages, [1,2,3,4,5])
        self.assertEqual(len(result['items']), 100)
        self.assertTrue(result['source_results'][0]['truncated'])
        self.assertTrue(result['source_results'][0]['baseline_eligible'])
        self.assertFalse(result['area_results'][0]['completed'])
        with self.assertRaises(ValueError): SearchInput(criteria=CRITERIA, limit=21)


if __name__ == '__main__':
    unittest.main()
