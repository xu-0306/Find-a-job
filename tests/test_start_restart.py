"""Restart ownership checks with synthetic processes; never uses the real database."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app.__main__ import main, stop_previous_backends


class RestartChecks(unittest.TestCase):
    def test_native_serve_loads_only_origin_and_preserves_explicit_environment(self):
        content = '# Deployment\nJOB_COMPANION_PUBLIC_ORIGIN=https://203.0.113.10:8443\nUNRELATED_SECRET=ignored\n'
        for explicit in (None, '', 'https://jobs.example.invalid'):
            environment = {} if explicit is None else {'JOB_COMPANION_PUBLIC_ORIGIN': explicit}
            with self.subTest(explicit=explicit), patch.dict(os.environ, environment, clear=True), \
                 patch('sys.argv', ['app', 'serve']), patch('pathlib.Path.is_file', return_value=True), \
                 patch('pathlib.Path.read_text', return_value=content), patch('uvicorn.run'), \
                 patch('app.__main__.read_config', return_value={'admin_port': 8041, 'local_port': 8088, 'public_port': 8443}):
                main()
                self.assertEqual(os.environ['JOB_COMPANION_PUBLIC_ORIGIN'],
                                 'https://203.0.113.10:8443' if explicit is None else explicit)
                self.assertNotIn('UNRELATED_SECRET', os.environ)

    def test_help_and_invalid_arguments_do_not_stop_backend(self):
        for args, code in [(['--help'], 0), (['--port', 'bad'], 2), (['--port', '65536'], 2)]:
            with self.subTest(args=args), patch('sys.argv', ['app', 'serve', '--restart', *args]), \
                 patch('app.__main__.stop_previous_backends') as stop, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_result:
                    main()
                self.assertEqual(exit_result.exception.code, code)
                stop.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows restart option')
    def test_shutdown_precedes_start_and_failure_prevents_start(self):
        with patch('sys.argv', ['app', 'serve', '--restart', '--port', '8042']), \
             patch('app.__main__.stop_previous_backends') as stop, patch('uvicorn.run') as serve:
            serve.side_effect = lambda *args, **kwargs: self.assertTrue(stop.called)
            main()
            self.assertEqual(serve.call_args.kwargs['port'], 8042)
            self.assertEqual(serve.call_args.kwargs['host'], '127.0.0.1')
            serve.reset_mock()
            stop.side_effect = subprocess.CalledProcessError(1, 'synthetic')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failed:
                main()
            self.assertEqual(failed.exception.code, 1)
            serve.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process ownership check')
    def test_only_owned_absolute_and_relative_backends_are_stopped(self):
        cache = Path(__file__).resolve().parents[1] / '.cache'
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='restart-', dir=cache) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(cache))
            roots = [Path(directory) / "Project A's space", Path(directory) / 'Other project']
            executables = []
            for root in roots:
                executable = root / '.venv' / 'Scripts' / 'python.exe'
                executable.parent.mkdir(parents=True)
                shutil.copyfile(sys.executable, executable)
                shutil.copyfile(Path(sys.prefix) / 'pyvenv.cfg', root / '.venv' / 'pyvenv.cfg')
                (root / 'app').mkdir()
                (root / 'app' / '__main__.py').write_text('import time\ntime.sleep(90)\n', encoding='utf-8')
                executables.append(executable)
            processes = []
            try:
                for executable, root, arguments in [
                    (executables[0], roots[0], ['-m', 'app', 'serve', '--port', '8042']),
                    (Path('.venv/Scripts/python.exe'), roots[0], ['-B', '-u', '-m', 'app', 'serve', '--port', '8043']),
                    (executables[1], roots[1], ['-m', 'app', 'serve']),
                    (executables[0], roots[0], ['-B', '-c', 'import time; time.sleep(90)', '-m', 'app', 'serve']),
                    (executables[1], roots[1], ['-B', '-m', 'app', 'serve']),
                    (executables[0], roots[0], ['-B', '-m', 'app', 'init-admin']),
                ]:
                    processes.append(subprocess.Popen([str(executable), *arguments], cwd=root,
                        executable=str(root / executable) if not executable.is_absolute() else str(executable),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW))
                time.sleep(.5)
                self.assertTrue(all(process.poll() is None for process in processes))
                pids = ','.join(str(process.pid) for process in processes)
                children = json.loads(subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                    "ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                    f'| Where-Object {{ $_.ParentProcessId -in @({pids}) }} | Select-Object ProcessId,ParentProcessId)']))
                with patch('sys.executable', str(executables[0])):
                    stop_previous_backends()
                for process in processes[:2]:
                    process.wait(timeout=5)
                owned = [str(child['ProcessId']) for child in children
                         if child['ParentProcessId'] in {process.pid for process in processes[:2]}]
                self.assertTrue(owned, 'check actual child workers as well as Python launchers')
                survivors = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                    f"@(Get-Process -Id {','.join(owned)} -ErrorAction SilentlyContinue).Count; exit 0"])
                self.assertEqual(survivors.strip(), b'0', 'old backend workers must exit too')
                self.assertTrue(all(process.poll() is None for process in processes[2:]),
                                'other projects and non-server Python processes must survive')
                with patch('sys.executable', str(executables[0])):
                    stop_previous_backends()  # No old backend is also a successful start.
            finally:
                for process in processes:
                    if process.poll() is None:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
