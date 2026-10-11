"""Port persistence, launcher precedence, and safe Funnel configuration changes."""
from datetime import timedelta
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from app.__main__ import main
from app.main import create_app
from app.ports import Ports, config_path, read_config, write_config
from app.sharing import FrontendOnly, Sharing, dns_origin, route_state
from app import security
from app.store import now

RUNNING = {'BackendState': 'Running', 'Self': {'DNSName': 'synthetic.example.ts.net.'}}


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class PortsContracts(unittest.TestCase):
    def test_legacy_config_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = config_path(directory)
            self.assertEqual(read_config(path), {'enabled': False, **Ports().model_dump()})
            path.write_text('{"enabled":true}', encoding='utf-8')
            self.assertEqual(read_config(path), {'enabled': True, **Ports().model_dump()})
            path.write_text('{"enabled":"false"}', encoding='utf-8')
            with self.assertRaises(ValueError):
                read_config(path)
        for values in ({'admin_port': 0}, {'local_port': 65536}, {'local_port': True},
                       {'admin_port': '8041'}, {'public_port': 8088}, {'public_port': 443.0},
                       {'admin_port': 8088}, {'unexpected': 1}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Ports(**values)

    def test_launcher_uses_saved_admin_port_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_DATA': directory}):
            write_config(config_path(), {'enabled': False, 'admin_port': 9041, 'local_port': 9088, 'public_port': 10000})
            for arguments, expected in (([], 9041), (['--port', '9042'], 9042)):
                with patch('sys.argv', ['app', 'serve', *arguments]), patch('uvicorn.run') as run:
                    main()
                    self.assertEqual(run.call_args.kwargs['port'], expected)
                    self.assertEqual(run.call_args.args[0].keywords['admin_port'], expected)

    def test_settings_api_auth_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            app = create_app(directory, automatic_daily=False, native_sharing=True)
            app.state.sharing.cli = lambda: 'synthetic'
            app.state.sharing.snapshot = AsyncMock(return_value=(RUNNING, {}))
            token = 'synthetic-admin-' + 'p' * 43
            with app.state.store.connect(write=True) as db:
                app.state.store.put(db, 'services', {'admin_local_only': False})
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), 'admin',
                    (now() + timedelta(hours=1)).isoformat(), None, None))
            values = {'admin_port': free_port(), 'local_port': free_port(), 'public_port': 10000}
            while values['admin_port'] == values['local_port']:
                values['local_port'] = free_port()
            with TestClient(app, base_url='http://localhost:8041', client=('127.0.0.1', 1)) as client:
                self.assertEqual(client.get('/api/admin/ports').status_code, 401)
                client.cookies.set('jc_admin', token)
                headers = {'Origin': 'http://localhost:8041'}
                self.assertEqual(client.put('/api/admin/ports', json=values, headers=headers).status_code, 403)
                headers['X-CSRF-Token'] = security.csrf(token)
                for invalid in ({**values, 'public_port': 8088}, {**values, 'local_port': values['admin_port']}):
                    self.assertEqual(client.put('/api/admin/ports', json=invalid, headers=headers).status_code, 422)
                response = client.put('/api/admin/ports', json=values, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['ports'], values)
                self.assertEqual(response.json()['current'], Ports().model_dump())
                self.assertTrue(response.json()['restart_required'])
                self.assertEqual(client.get('/api/admin/ports').json(), response.json())
            with TestClient(app, base_url='http://localhost:8041', client=('192.0.2.1', 1)) as client:
                client.cookies.set('jc_admin', token)
                self.assertEqual(client.get('/api/admin/ports').status_code, 403)
                self.assertEqual(client.put('/api/admin/ports', json=values, headers=headers).status_code, 403)

    def test_https_443_origin_route_and_frontend_host(self):
        origin = dns_origin(RUNNING, 443)
        self.assertEqual(origin, 'https://synthetic.example.ts.net')
        target = 'http://127.0.0.1:9088'
        routes = {'TCP': {'443': {'HTTPS': True}},
                  'Web': {'synthetic.example.ts.net:443': {'Handlers': {'/': {'Proxy': target}}}},
                  'AllowFunnel': {'synthetic.example.ts.net:443': True}}
        self.assertEqual(route_state(routes, origin, target), 'funnel')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            app = create_app(directory, automatic_daily=False)
            app.state.sharing.set_origin(origin)
            token = 'synthetic-user-' + 's' * 43
            with app.state.store.connect(write=True) as db:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), 'user',
                    (now() + timedelta(days=1)).isoformat(), 'synthetic', 'default'))
            with TestClient(FrontendOnly(app, app.state.sharing), base_url=origin) as client:
                self.assertEqual(client.get('/').status_code, 200)
                self.assertEqual(client.get('/', headers={'Host': 'synthetic.example.ts.net:443'}).status_code, 200)
                self.assertEqual(client.get('/admin').status_code, 403)
                self.assertEqual(client.get('/', headers={'Host': 'synthetic.example.ts.net:8443'}).status_code, 403)
                client.cookies.set('__Host-jc_user', token)
                self.assertEqual(client.post('/api/logout', headers={
                    'Host': 'synthetic.example.ts.net:443', 'Origin': origin,
                    'X-CSRF-Token': security.csrf(token)}).status_code, 204)


class PortChanges(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN': ''}):
            self.app = create_app(self.directory.name, automatic_daily=False)
        self.sharing = Sharing(self.app, native=True)
        self.sharing.cli = lambda: 'synthetic'
        self.sharing.snapshot = AsyncMock(return_value=(RUNNING, {}))

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_active_funnel_and_other_public_service_are_preserved(self):
        own = {'TCP': {'8443': {'HTTPS': True}},
               'Web': {'synthetic.example.ts.net:8443': {'Handlers': {'/': {'Proxy': self.sharing.target}}}},
               'AllowFunnel': {'synthetic.example.ts.net:8443': True}}
        other = {'TCP': {'443': {'HTTPS': True}}}
        for routes, values in ((own, {**self.sharing.ports, 'public_port': 10000}),
                               (other, {**self.sharing.ports, 'public_port': 443})):
            self.sharing.snapshot.return_value = (RUNNING, routes)
            before = json.dumps(routes)
            with patch('app.sharing.command', AsyncMock()) as command, self.assertRaises(ValueError):
                await self.sharing.save_ports(values)
            command.assert_not_awaited()
            self.assertEqual(json.dumps(routes), before)
            self.assertFalse(self.sharing.config_path.exists())

    async def test_occupied_local_port_does_not_save(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            values = {**self.sharing.ports, 'local_port': listener.getsockname()[1]}
            with self.assertRaisesRegex(ValueError, '占用'):
                await self.sharing.save_ports(values)
            self.assertFalse(self.sharing.config_path.exists())

    async def test_pending_changes_wait_for_restart_and_use_new_funnel_target(self):
        values = {**self.sharing.ports, 'local_port': free_port(), 'public_port': 10000}
        result = await self.sharing.save_ports(values)
        self.assertTrue(result['restart_required'])
        with patch('app.sharing.command', AsyncMock()) as command:
            self.assertTrue((await self.sharing.action('enable'))['restart_required'])
            command.assert_not_awaited()
        restarted = Sharing(self.app, native=True)
        restarted.cli = lambda: 'synthetic'
        restarted.listen = AsyncMock(return_value=True)
        restarted.probe = AsyncMock(return_value=True)
        target = f'http://127.0.0.1:{values["local_port"]}'
        own = {'TCP': {'10000': {'HTTPS': True}},
               'Web': {'synthetic.example.ts.net:10000': {'Handlers': {'/': {'Proxy': target}}}},
               'AllowFunnel': {'synthetic.example.ts.net:10000': True}}
        restarted.snapshot = AsyncMock(side_effect=[(RUNNING, {}), (RUNNING, own)])
        with patch('app.sharing.command', AsyncMock(return_value=(0, ''))) as command:
            response = await restarted.action('enable')
            self.assertEqual(response['state'], 'ready')
            self.assertEqual(response['origin'], 'https://synthetic.example.ts.net:10000')
            command.assert_awaited_once_with('synthetic', 'funnel', '--bg', '--yes', '--https=10000', target, timeout=20)
        self.assertEqual(read_config(restarted.config_path), {'enabled': True, **values})
        self.assertFalse(restarted.ports_view()['restart_required'])

    async def test_frontend_really_listens_on_the_saved_port(self):
        values = {**self.sharing.ports, 'local_port': free_port(), 'public_port': 10000}
        await self.sharing.save_ports(values)
        restarted = Sharing(self.app, native=True)
        restarted.set_origin(dns_origin(RUNNING, 10000))
        self.app.state.sharing = restarted
        try:
            self.assertTrue(await restarted.listen())
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                headers = {'Host': 'synthetic.example.ts.net:10000'}
                self.assertEqual((await client.get(restarted.target + '/', headers=headers)).status_code, 200)
                self.assertEqual((await client.get(restarted.target + '/admin', headers=headers)).status_code, 403)
                self.assertEqual((await client.get(restarted.target + '/')).status_code, 403)
        finally:
            await restarted.close()


if __name__ == '__main__':
    unittest.main()
