"""Funnel ownership, local-only setup, and streaming frontend boundary."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.main import create_app
from app import security
from app.sharing import FrontendOnly, Sharing, dns_origin, official_url, route_state
from app.ports import Ports
from app.store import now

ORIGIN = 'https://synthetic.example.ts.net:8443'
AUTHORITY = 'synthetic.example.ts.net:8443'
RUNNING = {'BackendState': 'Running', 'Self': {'DNSName': 'synthetic.example.ts.net.'}}
TARGET = 'http://127.0.0.1:8088'


def config(funnel=True):
    return {'TCP': {'443': {'HTTPS': True}, '11434': {'TCPForward': '127.0.0.1:11434'}, '8443': {'HTTPS': True}},
            'Web': {AUTHORITY: {'Handlers': {'/': {'Proxy': TARGET}}}}, 'AllowFunnel': {AUTHORITY: funnel}}


class SharingContracts(unittest.TestCase):
    def test_official_auth_urls_and_dns_are_validated(self):
        valid = 'https://login.tailscale.com/f/funnel?node=synthetic'
        self.assertEqual(official_url('Pending\n' + valid), valid)
        for value in ('https://evil.invalid/f/funnel', 'https://login.tailscale.com.evil.invalid/',
                      'https://a@login.tailscale.com/', 'https://login.tailscale.com:8088/'):
            self.assertIsNone(official_url(value))
        self.assertEqual(dns_origin(RUNNING, 8443), ORIGIN)
        for name in ('localhost', 'evil.ts.net.evil', 'name.ts.net/path', 'name_.ts.net', '.ts.net'):
            with self.assertRaises(ValueError):
                dns_origin({'Self': {'DNSName': name}}, 8443)

    def test_port_ownership_refuses_other_handlers_and_foreground(self):
        self.assertEqual(route_state({'TCP': {'443': {'HTTPS': True}}}, ORIGIN, TARGET), 'absent')
        self.assertEqual(route_state(config(), ORIGIN, TARGET), 'funnel')
        self.assertEqual(route_state(config(False), ORIGIN, TARGET), 'serve')
        for alter in ('target', 'path', 'authority', 'tcp', 'foreground'):
            data = config()
            if alter == 'target': data['Web'][AUTHORITY]['Handlers']['/']['Proxy'] = 'http://127.0.0.1:8041'
            if alter == 'path': data['Web'][AUTHORITY]['Handlers']['/other'] = {'Text': 'other'}
            if alter == 'authority': data['Web']['other.example.ts.net:8443'] = data['Web'].pop(AUTHORITY)
            if alter == 'tcp': data['TCP']['8443'] = {'TCPForward': '127.0.0.1:99'}
            if alter == 'foreground': data['Foreground'] = {'session': config()}
            self.assertEqual(route_state(data, ORIGIN, TARGET), 'conflict', alter)

    def test_frontend_blocks_admin_even_if_backend_local_only_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            app = create_app(directory, automatic_daily=False)
            sharing = app.state.sharing
            sharing.set_origin(ORIGIN)
            user = 'synthetic-user-' + 'u' * 43
            with app.state.store.connect(write=True) as db:
                app.state.store.put(db, 'services', {'admin_local_only': False})
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(user), 'user',
                    (now() + timedelta(days=1)).isoformat(), 'synthetic', 'default'))
            async def stream():
                yield b'data: first\n\n'
                await asyncio.sleep(.01)
                yield b'data: second\n\n'
            @app.get('/synthetic-stream')
            def synthetic_stream():
                return StreamingResponse(stream(), media_type='text/event-stream')
            with TestClient(FrontendOnly(app, sharing), base_url=ORIGIN) as client:
                self.assertEqual(client.get('/').status_code, 200)
                for path in ('/admin', '/admin/', '/api/admin/settings', '/%61dmin', '/api%2Fadmin/login-context'):
                    self.assertEqual(client.get(path).status_code, 403, path)
                for host in ('localhost:8041', '127.0.0.1:8088', 'evil.invalid', 'synthetic.example.ts.net'):
                    self.assertEqual(client.get('/', headers={'Host': host, 'X-Forwarded-Host': AUTHORITY}).status_code, 403)
                client.cookies.set('__Host-jc_user', user)
                response = client.get('/api/bootstrap', headers={'X-Forwarded-For': '127.0.0.1'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('Secure', response.headers['set-cookie'])
                self.assertEqual(client.get('/synthetic-stream').text, 'data: first\n\ndata: second\n\n')
                headers = {'X-CSRF-Token': security.csrf(user), 'Origin': 'http://' + AUTHORITY}
                self.assertEqual(client.post('/api/logout', headers=headers).status_code, 403)
                headers['Origin'] = ORIGIN
                self.assertEqual(client.post('/api/logout', headers=headers).status_code, 204)

    def test_sharing_setup_requires_local_admin_and_csrf(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            app = create_app(directory, automatic_daily=False)
            token = 'synthetic-admin-' + 'a' * 43
            with app.state.store.connect(write=True) as db:
                app.state.store.put(db, 'services', {'admin_local_only': False})
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), 'admin',
                    (now() + timedelta(hours=1)).isoformat(), None, None))
            with TestClient(app, base_url='http://localhost:8041', client=('127.0.0.1', 1)) as client:
                self.assertEqual(client.get('/api/admin/sharing').status_code, 401)
                client.cookies.set('jc_admin', token)
                headers = {'Origin': 'http://localhost:8041'}
                self.assertEqual(client.post('/api/admin/sharing', json={'action': 'enable'}, headers=headers).status_code, 403)
                headers['X-CSRF-Token'] = security.csrf(token)
                self.assertEqual(client.post('/api/admin/sharing', json={'action': 'reset'}, headers=headers).status_code, 422)
                self.assertEqual(client.post('/api/admin/sharing', json={'action': 'check'}, headers=headers).json()['state'], 'unsupported')
            with TestClient(app, base_url='http://localhost:8041', client=('192.0.2.1', 1)) as client:
                client.cookies.set('jc_admin', token)
                self.assertEqual(client.get('/api/admin/sharing', headers={'X-Forwarded-For': '127.0.0.1'}).status_code, 403)

    def test_live_api_uses_funnel_origin_instead_of_previous_manual_origin(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': 'https://203.0.113.10:8443'}):
            app = create_app(directory, automatic_daily=False, native_sharing=True)
            sharing = app.state.sharing
            sharing.cli = lambda: 'synthetic-tailscale'
            sharing.listen = AsyncMock(return_value=True)
            sharing.probe = AsyncMock(return_value=True)
            sharing.snapshot = AsyncMock(return_value=(RUNNING, config()))
            token = 'synthetic-admin-' + 'a' * 43
            with app.state.store.connect(write=True) as db:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), 'admin',
                    (now() + timedelta(hours=1)).isoformat(), None, None))
            with patch('app.sharing.command', AsyncMock(return_value=(0, ''))), TestClient(app, base_url='http://localhost:8041', client=('127.0.0.1', 1)) as client:
                client.cookies.set('jc_admin', token)
                headers = {'Origin': 'http://localhost:8041', 'X-CSRF-Token': security.csrf(token)}
                result = client.post('/api/admin/sharing', json={'action': 'enable'}, headers=headers)
                self.assertEqual(result.json()['origin'], ORIGIN)
                result = client.post('/api/admin/users/default/access/reset', headers=headers)
                self.assertEqual(result.json()['access_origin'], ORIGIN)
                self.assertEqual(result.status_code, 200)


class SharingActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            self.app = create_app(self.directory.name, automatic_daily=False)
        self.sharing = Sharing(self.app, native=True)
        self.sharing.cli = lambda: 'synthetic-tailscale'
        self.sharing.listen = AsyncMock(return_value=True)
        self.sharing.probe = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_enable_and_disable_only_our_8443_and_persist_startup(self):
        with patch('app.sharing.command', AsyncMock(return_value=(0, ''))) as command:
            self.sharing.snapshot = AsyncMock(side_effect=[(RUNNING, {}), (RUNNING, config())])
            result = await self.sharing.action('enable')
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(self.app.state.public_origin, ORIGIN)
            command.assert_awaited_once_with('synthetic-tailscale', 'funnel', '--bg', '--yes', '--https=8443', TARGET, timeout=20)
            self.assertEqual(json.loads(self.sharing.config_path.read_text()), {'enabled': True, **Ports().model_dump()})
            self.sharing.snapshot = AsyncMock(side_effect=[(RUNNING, config()), (RUNNING, {})])
            command.reset_mock()
            self.assertEqual((await self.sharing.action('disable'))['state'], 'disabled')
            command.assert_awaited_once_with('synthetic-tailscale', 'funnel', '--bg', '--https=8443', 'off')
            self.assertEqual(self.app.state.public_origin, '')
            self.assertEqual(json.loads(self.sharing.config_path.read_text()), {'enabled': False, **Ports().model_dump()})
        self.sharing.save(True)
        self.sharing.action = AsyncMock()
        await self.sharing.open()
        self.sharing.action.assert_awaited_once_with('enable')

    async def test_conflict_and_port_collision_never_run_funnel(self):
        conflict = config()
        conflict['Web'][AUTHORITY]['Handlers']['/']['Proxy'] = 'http://127.0.0.1:8041'
        for blocked_config, listening in ((conflict, True), ({}, False)):
            self.sharing.snapshot = AsyncMock(return_value=(RUNNING, blocked_config))
            self.sharing.listen.return_value = listening
            with patch('app.sharing.command', AsyncMock()) as command:
                result = await self.sharing.action('enable')
                self.assertIn(result['state'], ('conflict', 'error'))
                command.assert_not_awaited()

    async def test_consent_requires_human_and_does_not_claim_ready(self):
        self.sharing.snapshot = AsyncMock(return_value=(RUNNING, {}))
        with patch('app.sharing.command', AsyncMock(return_value=(-1,
                'synthetic secret ignored\nhttps://login.tailscale.com/f/funnel?node=synthetic'))):
            result = await self.sharing.action('enable')
        self.assertEqual(result['state'], 'consent_required')
        self.assertIsNone(result['origin'])
        self.assertEqual(result['action_url'], 'https://login.tailscale.com/f/funnel?node=synthetic')
        self.assertNotIn('synthetic secret', json.dumps(result))
        self.sharing.probe.assert_not_awaited()

    async def test_failed_https_probe_and_install_are_separate_states(self):
        self.sharing.snapshot = AsyncMock(return_value=(RUNNING, config()))
        self.sharing.probe.return_value = False
        self.assertEqual((await self.sharing.status())['state'], 'configured')
        self.sharing.cli = lambda: None
        future = asyncio.get_running_loop().create_future()
        self.sharing.install_task = future
        self.assertEqual((await self.sharing.status())['state'], 'installing')
        future.set_result((1, 'private installer output'))
        result = await self.sharing.status()
        self.assertEqual(result['state'], 'cli_missing')
        self.assertNotIn('private installer output', json.dumps(result))

    async def test_malformed_status_preserves_data_and_cannot_enable(self):
        self.sharing.snapshot = AsyncMock(side_effect=ValueError('synthetic secret'))
        result = await self.sharing.action('enable')
        self.assertEqual(result['state'], 'error')
        self.assertNotIn('synthetic secret', json.dumps(result))
        self.assertFalse(self.sharing.config_path.exists())


if __name__ == '__main__':
    unittest.main()
