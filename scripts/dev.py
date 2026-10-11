"""Local development only: independent data, no scheduler, Push, or Funnel."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare_environment():
    if Path(sys.prefix).resolve() != ROOT / '.venv':
        raise ValueError("Use this checkout's .venv Python (dev.cmd on Windows).")
    data = ROOT / '.dev-data'
    # Never follow a redirected development directory into another instance's data.
    if data.resolve() != data or (data / 'app.db').is_symlink():
        raise ValueError('Development data must stay in this checkout, without links.')
    os.environ['JOB_COMPANION_DATA'] = str(data)
    os.environ['JOB_COMPANION_PUBLIC_ORIGIN'] = ''
    os.environ['JOB_COMPANION_LOCAL_PEERS'] = ''
    return data


def blocked_push(*args, **kwargs):
    return {'result': 'blocked'}


def create_dev_app(port):
    data = prepare_environment()
    from app.main import create_app

    app = create_app(data, automatic_daily=False, native_sharing=False, admin_port=port)
    # Maintenance can reset the paused flag; disable the transport itself instead.
    app.state.notifications.sender = blocked_push
    return app


def serve(port):
    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        actual_port = listener.getsockname()[1]
        app = create_dev_app(actual_port)
        (ROOT / '.worktree-ports.json').write_text(
            json.dumps({'host': '127.0.0.1', 'admin_port': actual_port}) + '\n', encoding='utf-8')
        print(f'Development admin: http://127.0.0.1:{actual_port}/admin', flush=True)
        print(f'Data: {app.state.store.path.parent}', flush=True)
        print('Automatic daily searches, Push delivery and Funnel are disabled.', flush=True)
        config = uvicorn.Config(app, host='127.0.0.1', port=actual_port,
                                workers=1, access_log=False, proxy_headers=False)
        server = uvicorn.Server(config)
        server.run(sockets=[listener])
        if not server.started:
            raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8141, help='Loopback port; 0 selects a free port (default: 8141).')
    parser.add_argument('--init-admin', action='store_true', help='Set the development administrator password and exit.')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    try:
        prepare_environment()
        if args.init_admin:
            subprocess.run([sys.executable, '-m', 'app', 'init-admin'], cwd=ROOT, check=True)
        else:
            serve(args.port)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Development startup failed: {error}\n')


if __name__ == '__main__':
    main()
