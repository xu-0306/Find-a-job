"""Explicit HTTPS origin and Docker gateway never trust forwarded peer headers."""
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import security
from app.main import create_app
from app.store import now


class Deployment(unittest.TestCase):
    def test_local_admin_access_link_uses_configured_public_origin(self):
        for origin in ('', 'https://jobs.example.invalid', 'https://203.0.113.10:8443', 'https://[2001:db8::1]:9443'):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'JOB_COMPANION_PUBLIC_ORIGIN': origin, 'JOB_COMPANION_LOCAL_PEERS': ''}):
                app = create_app(directory, automatic_daily=False)
                token = 'synthetic-admin-' + 'a' * 40
                with app.state.store.connect(write=True) as db:
                    db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                               (security.digest(token), 'admin', (now() + timedelta(hours=1)).isoformat(), None, None))
                with TestClient(app, base_url='http://localhost:8041', client=('127.0.0.1', 1)) as local:
                    local.cookies.set('jc_admin', token)
                    result = local.post('/api/admin/users/default/access/reset', headers={
                        'Origin': 'http://localhost:8041', 'X-CSRF-Token': security.csrf(token),
                        'X-Forwarded-Host': 'evil.invalid', 'X-Forwarded-Proto': 'https'})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()['access_origin'], origin or None)
                    self.assertTrue(result.json()['access_path'].startswith('/access?token='))

    def test_public_host_secure_cookies_origin_and_local_admin_boundary(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JOB_COMPANION_PUBLIC_ORIGIN':'https://jobs.example.invalid', 'JOB_COMPANION_LOCAL_PEERS':'172.20.0.1'}):
            app = create_app(directory, automatic_daily=False)
            token = 'synthetic-user-'+'u'*40
            with app.state.store.connect(write=True) as db:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (security.digest(token), 'user', (now()+timedelta(days=1)).isoformat(), 'synthetic', 'default'))
            with TestClient(app, base_url='http://jobs.example.invalid', client=('172.20.0.1',1)) as client:
                client.headers['Cookie'] = '__Host-jc_user='+token
                response = client.get('/api/bootstrap')
                self.assertEqual(response.status_code,200,response.text)
                cookie = response.headers['set-cookie']
                self.assertIn('__Host-jc_user=',cookie)
                self.assertIn('Secure',cookie)
                self.assertIn('HttpOnly',cookie)
                self.assertEqual(client.get('/api/admin/login-context',headers={'X-Forwarded-For':'127.0.0.1','X-Forwarded-Host':'localhost'}).status_code,403)
                client.headers['X-CSRF-Token'] = security.csrf(token)
                self.assertEqual(client.post('/api/logout',headers={'Origin':'http://jobs.example.invalid'}).status_code,403)
                self.assertEqual(client.post('/api/logout',headers={'Origin':'https://jobs.example.invalid'}).status_code,204)
            with TestClient(app, base_url='http://localhost', client=('172.20.0.1',1)) as local:
                self.assertEqual(local.get('/api/admin/login-context').status_code,200)
            with TestClient(app, base_url='http://localhost', client=('172.20.0.2',1)) as remote:
                self.assertEqual(remote.get('/api/admin/login-context',headers={'X-Forwarded-For':'127.0.0.1'}).status_code,403)

    def test_invalid_environment_refused_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            for origin in ('http://jobs.example.invalid','https://jobs.example.invalid/path','https://user@jobs.example.invalid','https://jobs.example.invalid#fragment'):
                with patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN':origin}):
                    with self.assertRaises(ValueError): create_app(directory,automatic_daily=False)
            with patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN':'','JOB_COMPANION_LOCAL_PEERS':'172.20.0.0/16'}):
                with self.assertRaises(ValueError): create_app(directory,automatic_daily=False)

    def test_ip_custom_port_and_default_https_port_are_supported(self):
        for origin, authority in [('https://203.0.113.10:8443','203.0.113.10:8443'),
                                  ('https://203.0.113.10:443','203.0.113.10'),
                                  ('https://[2001:db8::1]:9443','[2001:db8::1]:9443')]:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'JOB_COMPANION_PUBLIC_ORIGIN':origin,'JOB_COMPANION_LOCAL_PEERS':''}):
                app = create_app(directory,automatic_daily=False)
                with TestClient(app,base_url='http://'+authority,client=('127.0.0.1',1)) as client:
                    with app.state.store.connect(write=True) as db:
                        token = 'synthetic-'+'t'*43
                        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(security.digest(token),'user',(now()+timedelta(days=1)).isoformat(),'device','default'))
                    response = client.get('/api/bootstrap',headers={'Cookie':'__Host-jc_user='+token})
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertIn('Secure',response.headers['set-cookie'])
                    self.assertEqual(client.get('/api/admin/login-context').status_code,403)


if __name__ == '__main__': unittest.main()
