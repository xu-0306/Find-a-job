"""Two accounts on one deployment must never see, change or delete each other's data.

Sessions are inserted directly, so these checks hold whatever way an account signs in.
"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import security
from app.main import TAIPEI, create_app
from app.store import Job, Store, encode, now

CRITERIA = {'keyword': '隔離測試', 'area_codes': [], 'min_monthly_salary': None}
ADMIN_TOKEN = 'a' * 43
SECRET = '只有爸爸看得到的私密內容'


def job(ident):
    return Job(source='104', source_job_id=ident, title='隔離職缺' + ident, company_name='合成公司',
               url='https://example.com/' + ident)


class IsolationChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, automatic_daily=False)
        self.store = self.app.state.store
        self.tokens = {'alice': 'b' * 43, 'bob': 'c' * 43}
        expiry = (now() + timedelta(days=1)).isoformat()
        with self.store.connect(write=True) as db:
            for name, token in self.tokens.items():
                db.execute('INSERT INTO users VALUES(?,?,NULL,0,1,?)', (name, name, now().isoformat()))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                           (security.digest(token), 'user', expiry, name + '-device', name))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (security.digest(ADMIN_TOKEN), 'admin', expiry, 'admin-device', None))
        self.clients = {}
        for number, (name, token) in enumerate(self.tokens.items(), start=1):
            self.clients[name] = self.open_client(number, 'jc_user', token)
        self.admin = self.open_client(9, 'jc_admin', ADMIN_TOKEN)
        self.alice, self.bob = self.clients['alice'], self.clients['bob']

    def open_client(self, number, cookie, token):
        client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', number))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.cookies.set(cookie, token)
        client.headers.update({'Origin': 'http://localhost', 'X-CSRF-Token': security.csrf(token)})
        return client

    def tearDown(self):
        self.directory.cleanup()

    def conversation_with_messages(self, client, request_id):
        ident = client.post('/api/conversations', json={'title': '私人對話'}).json()['id']
        with self.store.connect(write=True) as db:
            for number, (role, text) in enumerate([('user', SECRET), ('assistant', '收到')]):
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                           (f'{request_id}-{number}', request_id, role,
                            encode({'text': text, 'status': 'completed', 'events': []}), now().isoformat(), ident))
        return ident

    def test_conversations_and_messages_are_private(self):
        ident = self.conversation_with_messages(self.alice, 'alice-request')
        self.assertEqual([item['id'] for item in self.alice.get('/api/conversations').json()['items']], [ident])
        self.assertEqual(self.bob.get('/api/conversations').json()['items'], [])
        self.assertEqual(self.bob.get('/api/conversations', params={'q': '私密'}).json()['items'], [])
        self.assertEqual(self.bob.get('/api/messages').json()['items'], [])
        self.assertEqual(self.bob.get('/api/messages', params={'conversation_id': ident}).status_code, 404)
        self.assertEqual(self.bob.patch(f'/api/conversations/{ident}', json={'title': '被改名'}).status_code, 404)
        self.assertEqual(self.bob.delete(f'/api/conversations/{ident}').status_code, 404)
        self.assertEqual(self.bob.patch('/api/searches/any', json={
            'conversation_id': ident, 'job_id': 'x', 'dismissed': True}).status_code, 404)
        mine = self.alice.get('/api/messages', params={'conversation_id': ident}).json()['items']
        self.assertEqual(len(mine), 2)
        self.assertEqual(self.alice.get('/api/conversations').json()['items'][0]['title'], '私人對話')

    def test_chat_progress_and_cancel_belong_to_the_sender(self):
        self.conversation_with_messages(self.alice, 'alice-request')
        self.assertEqual(self.alice.get('/api/chat/alice-request').status_code, 200)
        self.assertEqual(self.bob.get('/api/chat/alice-request').status_code, 404)
        self.assertEqual(self.bob.post('/api/chat/alice-request/cancel').status_code, 404)
        self.assertEqual(self.alice.post('/api/chat/alice-request/cancel').status_code, 200)

    def test_favorites_are_per_account(self):
        with self.store.connect(write=True) as db:
            job_id = self.store.save_job(job('shared'), db)
        self.assertEqual(self.alice.put(f'/api/favorites/{job_id}').status_code, 200)
        self.assertEqual([item['id'] for item in self.alice.get('/api/favorites').json()['items']], [job_id])
        self.assertEqual(self.bob.get('/api/favorites').json()['items'], [])
        # Removing a favourite only removes the caller's own.
        self.assertEqual(self.bob.put(f'/api/favorites/{job_id}').status_code, 200)
        self.assertEqual(self.bob.delete(f'/api/favorites/{job_id}').status_code, 204)
        self.assertEqual([item['id'] for item in self.alice.get('/api/favorites').json()['items']], [job_id])
        self.assertEqual(self.bob.get('/api/favorites').json()['items'], [])

    def test_daily_jobs_read_state_and_favorite_flag_are_per_account(self):
        today = now().astimezone(TAIPEI).date().isoformat()
        with self.store.connect(write=True) as db:
            only_alice = self.store.save_job(job('alice-only'), db)
            both = self.store.save_job(job('both'), db)
            for ident, owners in ((only_alice, ['alice']), (both, ['alice', 'bob'])):
                for owner in owners:
                    db.execute('INSERT INTO daily_jobs VALUES(?,?,?,?,1,NULL)', (owner, today, ident, 'run'))
        self.assertEqual(self.alice.put(f'/api/favorites/{both}').status_code, 200)
        alice_items = {item['id']: item for item in self.alice.get('/api/daily-jobs').json()['items']}
        bob_items = {item['id']: item for item in self.bob.get('/api/daily-jobs').json()['items']}
        self.assertEqual(set(alice_items), {only_alice, both})
        self.assertEqual(set(bob_items), {both})
        self.assertTrue(alice_items[both]['is_favorite'])
        self.assertFalse(bob_items[both]['is_favorite'])
        # Reading someone else's entry fails, and reading your own never marks theirs.
        self.assertEqual(self.bob.post(f'/api/jobs/{only_alice}/read').status_code, 404)
        self.assertEqual(self.alice.post(f'/api/jobs/{both}/read').status_code, 200)
        self.assertIsNotNone({item['id']: item for item in self.alice.get('/api/daily-jobs').json()['items']}[both]['read_at'])
        self.assertIsNone(self.bob.get('/api/daily-jobs').json()['items'][0]['read_at'])

    def test_preferences_and_proposals_are_per_account(self):
        proposal = self.alice.post('/api/preference-proposals', json={'operation': 'create', 'after': CRITERIA})
        self.assertEqual(proposal.status_code, 201)
        proposal_id = proposal.json()['id']
        self.assertEqual([item['id'] for item in self.alice.get('/api/preferences').json()['proposals']], [proposal_id])
        self.assertEqual(self.bob.get('/api/preferences').json()['proposals'], [])
        self.assertEqual(self.bob.post(f'/api/preference-proposals/{proposal_id}/confirm').status_code, 404)
        self.assertEqual(self.bob.post(f'/api/preference-proposals/{proposal_id}/cancel').status_code, 404)
        self.assertEqual(self.alice.post(f'/api/preference-proposals/{proposal_id}/confirm').status_code, 200)
        self.assertEqual(len(self.alice.get('/api/preferences').json()['items']), 1)
        self.assertEqual(self.bob.get('/api/preferences').json()['items'], [])

    def test_disabled_account_loses_access_without_affecting_others(self):
        self.assertEqual(self.bob.get('/api/bootstrap').status_code, 200)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE users SET disabled=1 WHERE id='bob'")
        self.assertEqual(self.bob.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.alice.get('/api/bootstrap').status_code, 200)

    def test_each_account_may_chat_at_the_same_time(self):
        self.addCleanup(lambda: setattr(self.app.state, 'active_chats', {}))
        self.app.state.active_chats = {'alice': {'conversation_id': 'x', 'request_id': 'r'}}
        self.assertEqual(self.bob.post('/api/conversations', json={'title': '同時開新對話'}).status_code, 201)
        self.assertEqual(self.alice.post('/api/conversations', json={'title': '被擋下'}).status_code, 409)

    def test_deleting_an_account_removes_only_its_own_data(self):
        today = now().astimezone(TAIPEI).date().isoformat()
        self.conversation_with_messages(self.alice, 'alice-request')
        self.conversation_with_messages(self.bob, 'bob-request')
        with self.store.connect(write=True) as db:
            job_id = self.store.save_job(job('kept'), db)
            for owner in ('alice', 'bob'):
                db.execute('INSERT INTO favorites VALUES(?,?,?)', (job_id, owner, now().isoformat()))
                db.execute('INSERT INTO daily_jobs VALUES(?,?,?,?,1,NULL)', (owner, today, job_id, 'run'))
                db.execute('INSERT INTO preferences VALUES(?,1,?,1,?)', (owner + '-pref', encode(CRITERIA), owner))
        self.assertEqual(self.admin.delete('/api/admin/users/bob').status_code, 422)
        self.assertEqual(self.admin.delete('/api/admin/users/bob', params={'confirm': 'true'}).status_code, 204)
        with self.store.connect() as db:
            def count(table, owner):
                query = f'SELECT COUNT(*) FROM {table} WHERE user_id=?'
                return db.execute(query, (owner,)).fetchone()[0]
            for table in ('conversations', 'favorites', 'daily_jobs', 'preferences', 'sessions'):
                self.assertEqual(count(table, 'bob'), 0, table)
                self.assertGreater(count(table, 'alice'), 0, table)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE request_id='bob-request'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE request_id='alice-request'").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT 1 FROM users WHERE id='bob'").fetchone())
        self.assertEqual(self.bob.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.alice.get('/api/bootstrap').status_code, 200)

    # -- each account signs in through its own exclusive link ---------------------------------

    def visitor(self, number=20):
        client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', number))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def link(self, user_id):
        return self.admin.post(f'/api/admin/users/{user_id}/access/reset').json()['access_path']

    def open_link(self, path, number=20):
        client = self.visitor(number)
        return client, client.get(path, follow_redirects=False)

    def test_new_account_gets_its_own_link_and_starts_empty(self):
        created = self.admin.post('/api/admin/users', json={'username': 'carol'})
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertTrue(body['has_link'])
        self.assertNotIn('password', encode(body))
        token = body['access_path'].split('token=')[1]
        with self.store.connect() as db:
            stored = db.execute("SELECT access_token_hash FROM users WHERE username='carol'").fetchone()[0]
        self.assertEqual(stored, security.digest(token))
        self.assertNotEqual(stored, token)
        client, opened = self.open_link(body['access_path'])
        self.assertEqual(opened.status_code, 303)
        bootstrap = client.get('/api/bootstrap').json()
        self.assertEqual(bootstrap['account']['username'], 'carol')
        self.assertEqual(client.get('/api/conversations').json()['items'], [])
        self.assertEqual(client.get('/api/favorites').json()['items'], [])
        # The listing never shows a link again, only whether one exists.
        listed = {item['username']: item for item in self.admin.get('/api/admin/users').json()['items']}
        self.assertTrue(listed['carol']['has_link'])
        self.assertNotIn('access_path', listed['carol'])

    def test_usernames_are_unique_and_validated(self):
        self.assertEqual(self.admin.post('/api/admin/users', json={'username': 'ALICE'}).status_code, 409)
        self.assertEqual(self.admin.post('/api/admin/users', json={'username': 'a b'}).status_code, 422)
        self.assertEqual(self.admin.post('/api/admin/users', json={'username': 'ab'}).status_code, 422)
        self.assertEqual(self.admin.post('/api/admin/users', json={'username': 'dave', 'password': 'unused-field-12'}).status_code, 422)
        self.assertEqual(self.admin.patch('/api/admin/users/bob', json={'username': 'alice'}).status_code, 409)
        self.assertEqual(self.admin.patch('/api/admin/users/bob', json={'username': 'robert'}).json()['username'], 'robert')

    def test_each_link_opens_only_its_own_account(self):
        alice_path, bob_path = self.link('alice'), self.link('bob')
        self.assertNotEqual(alice_path, bob_path)
        alice, opened = self.open_link(alice_path, 21)
        self.assertEqual(opened.status_code, 303)
        self.assertEqual(alice.get('/api/bootstrap').json()['account']['username'], 'alice')
        bob, opened = self.open_link(bob_path, 22)
        self.assertEqual(opened.status_code, 303)
        self.assertEqual(bob.get('/api/bootstrap').json()['account']['username'], 'bob')

    def test_resetting_one_link_signs_out_only_that_account(self):
        alice_path, bob_path = self.link('alice'), self.link('bob')
        alice, _ = self.open_link(alice_path, 21)
        bob, _ = self.open_link(bob_path, 22)
        self.assertEqual(alice.get('/api/bootstrap').status_code, 200)
        replacement = self.link('alice')
        self.assertNotEqual(replacement, alice_path)
        self.assertEqual(alice.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.open_link(alice_path, 23)[1].status_code, 401)
        self.assertEqual(self.open_link(replacement, 24)[1].status_code, 303)
        self.assertEqual(bob.get('/api/bootstrap').status_code, 200)
        self.assertEqual(self.open_link(bob_path, 25)[1].status_code, 303)

    def test_disabled_account_link_is_refused_until_enabled_again(self):
        path = self.link('bob')
        bob, _ = self.open_link(path, 21)
        self.assertEqual(self.admin.patch('/api/admin/users/bob', json={'disabled': True}).json()['disabled'], True)
        self.assertEqual(bob.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.open_link(path, 22)[1].status_code, 401)
        self.assertEqual(self.admin.post('/api/admin/users/bob/access/reset').status_code, 409)
        self.assertEqual(self.admin.patch('/api/admin/users/bob', json={'disabled': False}).status_code, 200)
        self.assertEqual(self.open_link(path, 23)[1].status_code, 303)

    def test_removing_a_link_signs_the_account_out_and_voids_the_link(self):
        path = self.link('bob')
        bob, _ = self.open_link(path, 21)
        self.assertEqual(self.admin.delete('/api/admin/users/bob/access').status_code, 204)
        self.assertEqual(bob.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.open_link(path, 22)[1].status_code, 401)
        listed = {item['username']: item for item in self.admin.get('/api/admin/users').json()['items']}
        self.assertFalse(listed['bob']['has_link'])
        self.assertEqual(self.admin.delete('/api/admin/users/missing/access').status_code, 404)
        self.assertEqual(self.open_link(self.link('bob'), 23)[1].status_code, 303)

    def test_sign_out_all_devices_keeps_the_link_working(self):
        path = self.link('bob')
        bob, _ = self.open_link(path, 21)
        self.assertEqual(self.admin.post('/api/admin/users/bob/sign-out').status_code, 200)
        self.assertEqual(bob.get('/api/bootstrap').status_code, 401)
        self.assertEqual(self.open_link(path, 22)[1].status_code, 303)

    def test_unknown_or_malformed_links_and_accounts_without_a_link_are_refused(self):
        self.assertEqual(self.open_link('/access?token=' + 'z' * 43)[1].status_code, 401)
        self.assertEqual(self.open_link('/access?token=short')[1].status_code, 422)
        # alice and bob were created without a link: an empty hash must never match anything.
        self.assertEqual(self.open_link('/access?token=' + 'a' * 43)[1].status_code, 401)

    def test_admin_account_routes_need_the_admin_session(self):
        guest = self.visitor(31)
        guest.headers.update({'Origin': 'http://localhost'})
        for method, path, body in (('get', '/api/admin/users', None), ('post', '/api/admin/users', {'username': 'mallory'}),
                                   ('post', '/api/admin/users/bob/access/reset', None),
                                   ('delete', '/api/admin/users/bob/access', None),
                                   ('patch', '/api/admin/users/bob', {'disabled': True}),
                                   ('delete', '/api/admin/users/bob?confirm=true', None)):
            with self.subTest(path=path):
                result = getattr(guest, method)(path, **({'json': body} if body else {}))
                self.assertEqual(result.status_code, 401)
                result = getattr(self.alice, method)(path, **({'json': body} if body else {}))
                self.assertEqual(result.status_code, 401)

    def test_existing_owner_link_survives_the_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Store(Path(directory), target_version=5)
            token = 't' * 43
            with old.connect(write=True) as db:
                old.put(db, 'access_token_hash', security.digest(token))
            upgraded = create_app(directory, automatic_daily=False)
            with TestClient(upgraded, base_url='http://localhost', client=('127.0.0.1', 30)) as client:
                self.assertEqual(client.get('/access', params={'token': token}, follow_redirects=False).status_code, 303)
                self.assertEqual(client.get('/api/bootstrap').json()['account']['username'], 'owner')
            with upgraded.state.store.connect() as db:
                self.assertIsNone(upgraded.state.store.get(db, 'access_token_hash'))


if __name__ == '__main__':
    unittest.main()
