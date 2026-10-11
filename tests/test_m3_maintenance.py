"""Offline notification ownership, real ZIP round trip, and interrupted maintenance."""
import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import backups, notifications, push, security
from app.backup_verify import validate_backup
from app.main import create_app, finish_file_operation
from app.store import Job, Store, now
import test_m3_integration as integration
from app.store import encode


def subscription(suffix='one'):
    return {'endpoint': 'https://push.example.invalid/' + suffix,
            'keys': {'auth': 'YWJjZGVmZ2hpamtsbW5vcA', 'p256dh': push.generate_keys()['public_key']}}


class Maintenance(unittest.TestCase):
    tearDown, finish, run_daily = integration.Routes.tearDown, integration.Routes.finish, integration.Routes.run_daily

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name)/'data', automatic_daily=False)
        self.store = self.app.state.store
        self.fake = integration.OfflineSources()
        self.app.state.daily.sources = self.fake
        self.user, self.admin = 'u'*43, 'a'*43
        with self.store.connect(write=True) as db:
            for role, token in [('user', self.user), ('admin', self.admin)]:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), role,
                           (now()+timedelta(days=1)).isoformat(), 'synthetic-'+role, 'default' if role == 'user' else None))
            db.execute("UPDATE users SET has_chosen=1 WHERE id='default'")
            db.execute('INSERT INTO preferences VALUES(?,1,?,1,?)', ('p', encode(integration.CRITERIA), 'default'))
        self.client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1))
        self.client.__enter__()
        self.client.cookies.set('jc_admin', self.admin)
        self.client.cookies.set('jc_user', self.user)
        self.client.headers.update({'Origin':'http://localhost','X-CSRF-Token':security.csrf(self.admin)})

    def configure(self):
        with self.store.connect(write=True) as db:
            self.store.put(db, 'services', {'vapid_subject': 'mailto:synthetic@example.invalid'})
            self.store.put(db, 'admin_password_hash', security.password_hash('synthetic-only'))
        self.app.state.notifications.sender = lambda *args: {'result': 'accepted'}

    def bind(self, value):
        self.client.headers['X-CSRF-Token'] = security.csrf(self.user)
        response = self.client.post('/api/push-subscriptions', json=value)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def backup(self):
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        response = self.client.post('/api/admin/backups')
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id'], self.client.get('/api/admin/backups/'+response.json()['id']).content

    def restore(self, data, confirm=True):
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        return self.client.post('/api/admin/restore', data={'confirm': str(confirm).lower()},
                                files={'file': ('synthetic.zip', data, 'application/zip')})

    def test_subscription_ownership_rebind_logout_and_key_stability(self):
        self.configure()
        value = subscription()
        ident = self.bind(value)
        response = self.client.get('/api/bootstrap')
        self.assertTrue(response.json()['push_configured'])
        public_key = response.json()['push_public_key']
        for private in ('endpoint', 'keys_json', value['keys']['auth'], value['keys']['p256dh']):
            self.assertNotIn(private, response.text)
        second = 'v'*43
        with self.store.connect(write=True) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (security.digest(second), 'user', (now()+timedelta(days=1)).isoformat(), 'other-device', 'default'))
        self.client.cookies.clear()
        self.client.cookies.set('jc_admin', self.admin)
        self.client.cookies.set('jc_user', second)
        self.client.headers['X-CSRF-Token'] = security.csrf(second)
        self.assertEqual(self.client.delete('/api/push-subscriptions/'+ident).status_code, 204)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0], 1)
        rebound = self.client.post('/api/push-subscriptions', json=value).json()['id']
        self.assertEqual(rebound, ident)
        self.assertEqual(self.client.get('/api/bootstrap').json()['push_public_key'], public_key)
        self.assertEqual(self.client.post('/api/logout').status_code, 204)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0], 0)

    def test_daily_only_committed_new_rows_notify_once_and_push_test_reports_transport(self):
        self.configure()
        self.bind(subscription())
        calls = []
        self.app.state.notifications.sender = lambda *args: calls.append(args) or {'result': 'accepted'}
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        self.run_daily()
        self.assertEqual(calls, [])
        self.fake.items = ['old', 'new']
        second = self.run_daily()
        # Run status commits before transport; wait for the single worker to finish.
        for _ in range(200):
            if not self.app.state.daily.task: break
            time.sleep(.005)
        self.assertEqual(len(calls), 1)
        self.assertIn('2', calls[0][-1]['body'])
        self.run_daily()
        self.assertEqual(len(calls), 1)
        tested = self.client.post('/api/admin/push-test')
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(tested.json()['items'][0]['result'], 'accepted')
        with self.store.connect() as db:
            result = json.loads(db.execute('SELECT result_json FROM runs WHERE id=?', (second['id'],)).fetchone()[0])
            self.assertEqual(result['notifications']['accepted'], 1)

    def test_reset_waits_for_preparing_sender_and_prevents_post_reset_delivery(self):
        self.configure()
        self.bind(subscription())
        started, release = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            release.wait(2)
            return {'result':'accepted'}
        self.app.state.notifications.sender = slow
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        tests, resets = [], []
        sender = threading.Thread(target=lambda: tests.append(self.client.post('/api/admin/push-test')))
        sender.start()
        self.assertTrue(started.wait(1))
        reset = threading.Thread(target=lambda: resets.append(self.client.post('/api/admin/users/default/access/reset')))
        reset.start()
        time.sleep(.1)
        self.assertTrue(reset.is_alive())
        self.assertTrue(self.app.state.notifications.paused)
        release.set()
        sender.join(3)
        reset.join(3)
        self.assertEqual(tests[0].status_code,200)
        self.assertEqual(resets[0].status_code,200)
        self.assertEqual(self.app.state.notifications.active,0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0],0)

    def test_write_authenticated_before_reset_is_rejected_inside_transaction(self):
        self.configure()
        job = self.store.save_job(Job(source='104',source_job_id='race',title='合成',company_name='合成',url='https://example.invalid/job'))
        original = self.store.connect
        for route, method, body in [('/api/favorites/'+job,'put',None),
                                    ('/api/preference-proposals','post',{'operation':'create','after':integration.CRITERIA})]:
            with original(write=True) as db:
                db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',
                    (security.digest(self.user),'user',(now()+timedelta(days=1)).isoformat(),'synthetic-user','default'))
            started, release = threading.Event(), threading.Event()
            counter = 0
            @contextmanager
            def paused_transaction(write=False):
                nonlocal counter
                if write:
                    counter += 1
                    if counter == 2:
                        started.set()
                        release.wait(3)
                with original(write=write) as db: yield db
            results = []
            self.client.headers['X-CSRF-Token'] = security.csrf(self.user)
            with patch.object(self.store,'connect',paused_transaction):
                old = threading.Thread(target=lambda: results.append(getattr(self.client,method)(route,json=body)))
                old.start()
                self.assertTrue(started.wait(1))
                revoked = self.client.post('/api/admin/users/default/access/reset',headers={'X-CSRF-Token':security.csrf(self.admin)})
                self.assertEqual(revoked.status_code,200,revoked.text)
                release.set()
                old.join(3)
            self.assertEqual(results[0].status_code,401,results[0].text)
        with original() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM favorites').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM preference_proposals').fetchone()[0],0)

    def test_real_backup_restore_preserves_settings_sprites_data_revoke_all_devices(self):
        self.configure()
        self.bind(subscription())
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        self.run_daily()
        payload = io.BytesIO()
        Image.new('RGBA', (8,4)).save(payload, format='PNG')
        fields = dict(frame_width='4', frame_height='4', frame_count='2', fps='8')
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin)
        self.assertEqual(self.client.put('/api/admin/sprites/idle', data=fields,
                        files={'file': ('sprite.png', payload.getvalue(), 'image/png')}).status_code, 200)
        ident, data = self.backup()
        self.assertEqual(self.client.get('/api/admin/backups').json()['items'][0]['id'], ident)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['schema_version'], 7)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)/'backup.zip'
            archive.write_bytes(data)
            stage = Path(directory)/'stage'
            stage.mkdir()
            with self.store.connect() as db: schema = backups.schema_catalog(db)
            validate_backup(archive, stage, schema)
            with closing(sqlite3.connect(stage/'app.db')) as db:
                for table in ('sessions', 'login_attempts', 'push_subscriptions', 'push_attempts'):
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
        with self.store.connect(write=True) as db:
            keys = self.store.get(db, 'vapid_keys')
            self.store.put(db, 'schedule_time', '10:21')
            db.execute('DELETE FROM quotes')
            db.execute("INSERT INTO quotes VALUES('q','only after backup',1)")
        def stored_data():
            with self.store.connect() as db:
                return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                        for table in ('settings','jobs','daily_jobs','sprites','quotes')}
        original_data = stored_data()
        self.assertEqual(self.restore(data, False).status_code, 422)
        self.assertEqual(self.restore(b'not ZIP').status_code, 422)
        self.assertEqual(stored_data(), original_data)
        restored = self.restore(data)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertTrue(restored.json()['resubscribe_required'])
        self.assertTrue((self.store.path.parent/'backups'/(restored.json()['pre_restore_backup_id']+'.zip')).is_file())
        with self.store.connect() as db:
            self.assertEqual(self.store.get(db, 'vapid_keys'), keys)
            self.assertIsNone(self.store.get(db, 'schedule_time'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quotes').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0], 0)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            sprite = db.execute('SELECT path FROM sprites').fetchone()[0]
            self.assertEqual((self.store.path.parent/'sprites'/sprite).read_bytes(), payload.getvalue())
        self.assertEqual(self.client.get('/api/admin/settings').status_code, 401)
        with TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1)) as anonymous:
            self.assertEqual(anonymous.get('/api/admin/backups/'+ident).status_code, 401)

    def test_failure_after_each_replace_and_revoke_rolls_back_original_sessions_and_data(self):
        self.configure()
        _, data = self.backup()
        with self.store.connect(write=True) as db:
            self.store.put(db, 'schedule_time', '10:21')
        for point in ('backup_created', 'database_replaced', 'sprites_replaced', 'before_session_revoke', 'after_session_revoke', 'restored'):
            def fail(stage):
                if stage == point: raise OSError('synthetic disk failure')
            self.app.state.restore_checkpoint = fail
            response = self.restore(data)
            self.assertEqual(response.status_code, 503, (point, response.text))
            self.assertIn('原資料已恢復', response.text)
            self.assertEqual(self.client.get('/api/admin/session').status_code, 200)
            with self.store.connect() as db:
                self.assertEqual(self.store.get(db, 'schedule_time'), '10:21')
            self.assertFalse(self.app.state.maintenance)

    def test_maintenance_blocks_admissions_while_actual_filesystem_thread_runs(self):
        self.configure()
        started, release = threading.Event(), threading.Event()
        original = backups.create_backup
        def slow(store):
            started.set()
            release.wait(2)
            return original(store)
        responses = []
        with patch.object(backups, 'create_backup', slow):
            thread = threading.Thread(target=lambda: responses.append(self.client.post('/api/admin/backups')))
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(self.app.state.daily.paused)
            for route in ('/api/bootstrap', '/api/admin/session', '/api/daily-jobs'):
                self.assertEqual(self.client.get(route).status_code, 409)
            self.assertEqual(self.client.post('/api/admin/search-runs').status_code, 409)
            release.set()
            thread.join(3)
        self.assertEqual(responses[0].status_code, 201)
        self.assertFalse(self.app.state.maintenance)

    def test_compensation_failure_stops_all_new_writers_and_retains_recovery(self):
        self.configure()
        _, data = self.backup()
        replace = backups.os.replace
        def fail_compensation(source, destination):
            if Path(source).name == 'app.db' and Path(source).parent.name.startswith('job-companion-recovery-'):
                raise OSError('synthetic compensation failure')
            return replace(source, destination)
        def fail(stage):
            if stage == 'database_replaced': raise OSError('synthetic replace failure')
        self.app.state.restore_checkpoint = fail
        with patch.object(backups.os, 'replace', fail_compensation):
            response = self.restore(data)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertTrue(self.app.state.recovery_required)
        self.assertTrue(self.app.state.maintenance)
        self.assertTrue(self.app.state.daily.paused)
        self.assertEqual(self.client.get('/api/bootstrap').status_code, 409)
        recovery = list(self.store.path.parent.glob('job-companion-recovery-*'))
        self.assertEqual(len(recovery), 1)
        self.assertTrue((recovery[0]/'app.db').is_file())


class AsyncChecks(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_file_operation_does_not_release_until_thread_stops(self):
        started, release = threading.Event(), threading.Event()
        def slow():
            started.set()
            release.wait(2)
        task = asyncio.create_task(finish_file_operation(slow))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task

        started.clear()
        release.clear()
        def failed_restore():
            started.set()
            release.wait(2)
            raise backups.RestoreError('synthetic', recovery_required=True)
        task = asyncio.create_task(finish_file_operation(failed_restore))
        await asyncio.to_thread(started.wait,1)
        task.cancel()
        release.set()
        with self.assertRaises(backups.RestoreError) as caught: await task
        self.assertTrue(caught.exception.recovery_required)

    async def test_claim_once_expiration_rebind_late_response_and_cancel_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            notifications.ensure_keys(store)
            value = subscription()
            with store.connect(write=True) as db:
                store.put(db, 'services', {'vapid_subject':'mailto:synthetic@example.invalid'})
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('user', 'user', (now()+timedelta(days=1)).isoformat(), 'device', 'default'))
                db.execute('INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?)', ('sub', 'device', value['endpoint'], json.dumps(value['keys']), now().isoformat(), 'default'))
            service = notifications.Notifications(store)
            calls = []
            service.sender = lambda *args: calls.append(args) or {'result':'unknown'}
            self.assertEqual(await service.deliver('zero', 0), [])
            await service.deliver('run', 1)
            await service.deliver('run', 1)
            self.assertEqual(len(calls), 1)
            started, release = threading.Event(), threading.Event()
            def slow(*args):
                started.set()
                release.wait(2)
                return {'result':'expired'}
            service.sender = slow
            task = asyncio.create_task(service.deliver('late', 1))
            await asyncio.to_thread(started.wait, 1)
            with store.connect(write=True) as db:
                db.execute("UPDATE push_subscriptions SET keys_json=? WHERE id='sub'", (json.dumps(subscription()['keys']),))
            task.cancel()
            await asyncio.sleep(.02)
            self.assertEqual(service.active, 1)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT result FROM push_attempts WHERE run_id='late'").fetchone()[0], 'unknown')
            service.sender = lambda *args: {'result':'expired'}
            await service.deliver('expired', 1)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
