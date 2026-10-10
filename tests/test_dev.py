"""Development cannot inherit a production database, route, or Push transport."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts import dev


class DevelopmentChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='isolated dev ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.contexts = [
            patch.object(dev, 'ROOT', self.root),
            patch.object(dev.sys, 'prefix', str(self.root / '.venv')),
            patch.dict(os.environ, {
                'JOB_COMPANION_DATA': str(self.root / 'production'),
                'JOB_COMPANION_PUBLIC_ORIGIN': 'https://production.invalid',
                'JOB_COMPANION_LOCAL_PEERS': '192.0.2.50',
            }),
        ]
        for context in self.contexts:
            context.start()
            self.addCleanup(context.stop)

    def test_startup_uses_private_data_and_disables_background_side_effects(self):
        production = self.root / 'production'
        production.mkdir()
        sentinel = production / 'app.db'
        sentinel.write_bytes(b'untouched production sentinel')
        app = dev.create_dev_app(12345)
        with patch('app.sharing.command', side_effect=AssertionError('No Tailscale')), \
             patch('app.push.send', side_effect=AssertionError('No real Push')), \
             TestClient(app, base_url='http://127.0.0.1:12345', client=('127.0.0.1', 45000)) as client:
            self.assertEqual(client.get('/admin').status_code, 200)
            self.assertEqual(app.state.store.path, self.root / '.dev-data' / 'app.db')
            self.assertEqual(app.state.public_origin, '')
            self.assertFalse(app.state.sharing.native)
            self.assertFalse(app.state.daily.automatic)
            self.assertIsNone(app.state.daily.scheduler)
            self.assertIsNone(app.state.daily.task)
            app.state.notifications.paused = False
            self.assertEqual(app.state.notifications.sender({}, '', '', {}), {'result': 'blocked'})
        self.assertEqual(sentinel.read_bytes(), b'untouched production sentinel')

    def test_foreign_python_is_rejected_before_any_database_is_created(self):
        with patch.object(dev.sys, 'prefix', str(self.root / 'another environment')):
            with self.assertRaisesRegex(ValueError, 'checkout'):
                dev.prepare_environment()
        self.assertFalse((self.root / '.dev-data').exists())

    def test_occupied_port_fails_without_creating_application(self):
        with socket.socket() as occupied:
            if os.name == 'nt':
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            with patch.object(dev, 'create_dev_app') as create:
                with self.assertRaises(OSError):
                    dev.serve(occupied.getsockname()[1])
                create.assert_not_called()
            self.assertGreater(occupied.fileno(), -1)


if __name__ == '__main__':
    unittest.main()
