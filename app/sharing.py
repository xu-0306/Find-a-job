"""Native Windows Funnel setup and a frontend-only loopback listener."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
from urllib.parse import urlsplit

import httpx
from starlette.responses import Response
import uvicorn

from .ports import Ports, read_config, write_config


def official_url(output):
    for candidate in re.findall(r'https://[^\s<>"\x00-\x1f]+', output):
        try:
            url = urlsplit(candidate)
            if (url.hostname == 'login.tailscale.com' and url.port in (None, 443)
                    and not url.username and not url.password and not url.fragment):
                return candidate
        except ValueError:
            pass
    return None


def dns_origin(status, public_port):
    name = status.get('Self', {}).get('DNSName', '').rstrip('.').lower()
    if (not name.endswith('.ts.net') or len(name) > 253
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                       for part in name.split('.'))):
        raise ValueError('Invalid Tailscale DNS name')
    return f'https://{name}' + (f':{public_port}' if public_port != 443 else '')


def route_state(config, origin, target):
    """Only our exact port, authority, and single proxy handler may be changed."""
    url = urlsplit(origin)
    port = url.port or 443
    authority = f'{url.hostname}:{port}'
    tcp = config.get('TCP', {}).get(str(port))
    web = {key: value for key, value in config.get('Web', {}).items()
           if key.rsplit(':', 1)[-1] == str(port)}
    if any(route_state(foreground, origin, target) != 'absent'
           for foreground in config.get('Foreground', {}).values()):
        return 'conflict'
    if tcp is None and not web and not config.get('AllowFunnel', {}).get(authority):
        return 'absent'
    if tcp != {'HTTPS': True} or web != {authority: {'Handlers': {'/': {'Proxy': target}}}}:
        return 'conflict'
    return 'funnel' if config.get('AllowFunnel', {}).get(authority) is True else 'serve'


async def command(*arguments, timeout=15):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE, creationflags=flags)
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        stdout, stderr = await output
        if asyncio.current_task().cancelling():
            raise
    return process.returncode, (stdout + b'\n' + stderr).decode('utf-8', errors='replace')


class FrontendOnly:
    def __init__(self, app, sharing):
        self.app, self.sharing = app, sharing

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        headers = scope.get('headers', [])
        hosts = [value.decode('latin-1').lower() for key, value in headers if key == b'host']
        origin = self.sharing.origin
        path = scope.get('path', '')
        admin_path = path == '/admin' or path.startswith('/admin/') or path == '/api/admin' or path.startswith('/api/admin/')
        allowed_hosts = {urlsplit(origin).netloc} if origin else set()
        if origin and urlsplit(origin).port is None:
            allowed_hosts.add(urlsplit(origin).netloc + ':443')
        if (scope['type'] != 'http' or len(hosts) != 1 or hosts[0] not in allowed_hosts
                or admin_path or path.startswith('//') or '\\' in path):
            await Response(status_code=403, headers={'Cache-Control': 'no-store'})(scope, receive, send)
            return
        scope = dict(scope, scheme='https', client=('funnel', 0))
        scope['headers'] = [(key, urlsplit(origin).netloc.encode('ascii') if key == b'host' else value) for key, value in headers
                            if key != b'forwarded' and not key.startswith(b'x-forwarded-')
                            and key != b'tailscale-user-login' and key != b'tailscale-user-name'
                            and key != b'tailscale-user-profile-pic']
        await self.app(scope, receive, send)


class FrontendServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The native backend owns shutdown; this listener shares its event loop.
        yield


class Sharing:
    def __init__(self, app, *, native=False, admin_port=None):
        self.app, self.native = app, native
        self.origin = None
        self.config_path = app.state.store.path.parent / 'funnel.json'
        config = read_config(self.config_path)
        self.ports = {name: config[name] for name in Ports.model_fields}
        if admin_port is not None:
            self.ports['admin_port'] = admin_port
        self.lock = asyncio.Lock()
        self.server = self.server_task = self.install_task = None
        self.listener_error = False
        self.action_url = None
        self.last_error = None

    def cli(self):
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Tailscale' / 'tailscale.exe'
        return str(candidate) if candidate.is_file() else shutil.which('tailscale')

    def set_origin(self, value):
        self.origin = value
        self.app.state.public_origin = value or ''

    def save(self, enabled):
        write_config(self.config_path, {**read_config(self.config_path), 'enabled': enabled})

    @property
    def target(self):
        return f'http://127.0.0.1:{self.ports["local_port"]}'

    def sharing_restart_required(self):
        config = read_config(self.config_path)
        return any(config[name] != self.ports[name] for name in ('local_port', 'public_port'))

    def ports_view(self):
        config = read_config(self.config_path)
        saved = {name: config[name] for name in Ports.model_fields}
        return {'ports': saved, 'current': self.ports, 'editable': self.native,
                'restart_required': saved != self.ports}

    async def save_ports(self, ports):
        if not self.native:
            raise ValueError('此區只適用 Windows 本機啟動；容器請修改部署連接埠設定。')
        if self.lock.locked():
            raise ValueError('另一個分享操作正在進行，請稍候再保存。')
        async with self.lock:
            config = read_config(self.config_path)
            if any(ports[name] != self.ports[name] for name in ('local_port', 'public_port')):
                if self.install_task and not self.install_task.done():
                    raise ValueError('請等 Tailscale 安裝完成後再更改前台或公開埠。')
                if self.cli():
                    try:
                        status, routes = await self.snapshot()
                    except (OSError, ValueError, TypeError, AttributeError) as error:
                        raise ValueError('無法確認 Tailscale 連接埠，請啟動 Tailscale 並重新檢查後再保存。') from error
                    if status.get('BackendState') != 'Running' and config['enabled']:
                        raise ValueError('請先啟動 Tailscale，停用下方的 Funnel 後再更改前台或公開埠。')
                    if status.get('BackendState') == 'Running':
                        origin = dns_origin(status, self.ports['public_port'])
                        if route_state(routes, origin, self.target) in ('funnel', 'serve'):
                            raise ValueError('更改前台或公開埠前，請先在下方「前台專屬連結」停用 Funnel。')
                        origin = dns_origin(status, ports['public_port'])
                        target = f'http://127.0.0.1:{ports["local_port"]}'
                        if route_state(routes, origin, target) != 'absent':
                            raise ValueError(f'Tailscale 公開埠 {ports["public_port"]} 已有服務設定，請選擇其他埠。')
                elif config['enabled']:
                    raise ValueError('請先安裝並啟動 Tailscale，確認已停用 Funnel 後再更改前台或公開埠。')
            owned = {self.ports['admin_port']}
            if self.server_task and not self.server_task.done():
                owned.add(self.ports['local_port'])
            for name in ('admin_port', 'local_port'):
                port = ports[name]
                if port == self.ports[name] or port in owned:
                    continue
                with socket.socket() as listener:
                    try:
                        if os.name == 'nt':
                            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                        listener.bind(('127.0.0.1', port))
                    except OSError as error:
                        raise ValueError(f'本機連接埠 {port} 已被占用或不允許使用，請選擇其他埠。') from error
            write_config(self.config_path, {**config, **ports})
            if self.sharing_restart_required():
                self.set_origin(None)
            return self.ports_view()

    def view(self, state, message, *, can_disable=False):
        action_url = self.action_url if state in ('cli_missing', 'needs_login', 'consent_required') else None
        return {'state': state, 'message': message, 'origin': self.origin,
                'action_url': action_url, 'can_disable': can_disable,
                'local_port': self.ports['local_port'], 'public_port': self.ports['public_port'],
                'restart_required': self.sharing_restart_required()}

    async def open(self):
        if not self.native:
            return
        if self.config_path.exists():
            self.set_origin(None)
            try:
                enabled = read_config(self.config_path)['enabled']
            except (OSError, ValueError, KeyError, TypeError):
                self.last_error = '分享設定無法讀取，請在此重新啟用；本機前台仍可使用。'
                return
            if enabled:
                await self.action('enable')

    async def listen(self):
        if self.server_task and not self.server_task.done():
            return True
        listener = socket.socket()
        try:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', self.ports['local_port']))
            listener.setblocking(False)
            self.server = FrontendServer(uvicorn.Config(FrontendOnly(self.app, self), access_log=False,
                proxy_headers=False, lifespan='off', log_config=None, log_level=None, timeout_graceful_shutdown=3))
            self.server_task = asyncio.create_task(self.server.serve(sockets=[listener]))
            # Binding is reserved before Funnel is configured; wait for HTTP readiness.
            while not self.server.started and not self.server_task.done():
                await asyncio.sleep(.01)
            self.listener_error = self.server_task.done()
        except OSError:
            listener.close()
            self.listener_error = True
        return not self.listener_error

    async def close(self):
        if self.server:
            self.server.should_exit = True
            await self.server_task

    async def snapshot(self):
        cli = self.cli()
        code, output = await command(cli, 'status', '--json')
        if code:
            raise OSError('Tailscale daemon unavailable')
        status = json.loads(output)
        if not isinstance(status, dict):
            raise ValueError('Invalid status')
        config = {}
        if status.get('BackendState') == 'Running':
            code, output = await command(cli, 'serve', 'status', '--json')
            if code:
                raise OSError('Tailscale Serve unavailable')
            config = json.loads(output)
            if not isinstance(config, dict):
                raise ValueError('Invalid Serve config')
        return status, config

    async def probe(self):
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
                home = await client.get(self.origin + '/')
                denied = await client.get(self.origin + '/api/admin/login-context')
                return (home.status_code == 200 and denied.status_code == 403
                        and home.content == (Path(__file__).parent / 'static' / 'index.html').read_bytes())
        except httpx.HTTPError:
            return False

    async def status(self):
        if not self.native:
            return self.view('unsupported', '自動分享目前支援 Windows 本機啟動；其他部署請使用部署說明。')
        if self.sharing_restart_required():
            return self.view('disabled', '新連接埠已保存，請重新執行 start.cmd，再啟用 Funnel。')
        if self.install_task:
            if not self.install_task.done():
                return self.view('installing', '正在安裝 Tailscale；若 Windows 詢問權限，請允許安裝。')
            try:
                code, _ = self.install_task.result()
                if code:
                    self.last_error = 'Tailscale 安裝未完成，請重新檢查或從官方網站安裝。'
            except (OSError, asyncio.CancelledError):
                self.last_error = '無法完成安裝，請從 Tailscale 官方網站安裝後重新檢查。'
            self.install_task = None
        if not self.cli():
            self.action_url = 'https://tailscale.com/download/windows'
            return self.view('cli_missing', self.last_error or '尚未偵測到 Tailscale。安裝並登入後，就能分享前台。')
        try:
            status, config = await self.snapshot()
            if self.action_url and urlsplit(self.action_url).hostname != 'login.tailscale.com':
                self.action_url = None
            backend = status.get('BackendState')
            if backend not in ('Running', 'Stopped', 'NeedsLogin', 'NeedsMachineAuth', 'NoState', 'Starting'):
                raise ValueError('Unknown backend state')
            if backend != 'Running':
                if self.config_path.exists():
                    self.set_origin(None)
                self.action_url = official_url(status.get('AuthURL', '')) or self.action_url
                state = 'stopped' if backend == 'Stopped' else 'needs_login'
                return self.view(state, 'Tailscale 尚未連線，請登入或啟動後重新檢查。')
            origin = dns_origin(status, self.ports['public_port'])
            route = route_state(config, origin, self.target)
            if route == 'conflict':
                if self.config_path.exists():
                    self.set_origin(None)
                return self.view('conflict', f'Tailscale 的 {self.ports["public_port"]} 已供其他服務使用；請在上方連接埠卡片選擇其他公開埠。')
            if route != 'funnel':
                if self.config_path.exists():
                    self.set_origin(None)
                state = 'consent_required' if self.action_url and '/f/funnel' in self.action_url else 'disabled'
                return self.view(state, '請在 Tailscale 官方頁面允許 Funnel，再回來重新啟用。' if state == 'consent_required'
                                 else self.last_error or 'Tailscale 已登入，尚未開啟這個前台的外部連線。', can_disable=route == 'serve')
            self.action_url = None
            self.set_origin(origin)
            if not await self.listen():
                self.set_origin(None)
                return self.view('error', f'本機 {self.ports["local_port"]} 已被占用，前台分享入口無法啟動；請停用 Funnel，再到上方連接埠卡片改埠。', can_disable=True)
            if await self.probe():
                return self.view('ready', '外部連線已啟用，前台 HTTPS 測試通過。', can_disable=True)
            return self.view('configured', 'Funnel 已設定，HTTPS 網址仍未通過測試。首次啟用可能需要等候 DNS 更新，請稍後再次測試。', can_disable=True)
        except OSError:
            return self.view('daemon_unavailable', '無法連接 Tailscale 服務；請開啟電腦上的 Tailscale，再重新檢查。')
        except (ValueError, TypeError, AttributeError):
            return self.view('error', '無法辨識 Tailscale 狀態，請更新 Tailscale 後重新檢查。')

    async def action(self, action):
        if self.lock.locked():
            return self.view('error', '另一個分享操作正在進行，請稍候再重新檢查。')
        async with self.lock:
            if not self.native:
                return await self.status()
            if self.sharing_restart_required():
                return await self.status()
            self.last_error = None
            if action == 'install':
                if not self.cli() and not self.install_task:
                    winget = shutil.which('winget')
                    if not winget:
                        self.last_error = '這台電腦沒有 Windows 套件管理員，請使用下方官方安裝入口。'
                    else:
                        self.install_task = asyncio.create_task(command(winget, 'install', '--id', 'Tailscale.Tailscale',
                            '--exact', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements', timeout=240))
                return await self.status()
            if action == 'check':
                return await self.status()
            if not self.cli():
                return await self.status()
            try:
                status, config = await self.snapshot()
                if action == 'login':
                    if status.get('BackendState') != 'Running':
                        _, output = await command(self.cli(), 'up', '--timeout=15s', timeout=18)
                        self.action_url = official_url(output)
                        if not self.action_url:
                            self.last_error = '若尚未連線，請從電腦的 Tailscale 圖示登入，再重新檢查。'
                    return await self.status()
                if status.get('BackendState') != 'Running':
                    return await self.status()
                origin = dns_origin(status, self.ports['public_port'])
                route = route_state(config, origin, self.target)
                if route == 'conflict':
                    return await self.status()
                if action == 'disable':
                    if route in ('funnel', 'serve'):
                        code, _ = await command(self.cli(), 'funnel', '--bg', f'--https={self.ports["public_port"]}', 'off')
                        if code:
                            return self.view('error', '無法停用 Funnel，請重新檢查目前狀態。', can_disable=True)
                    self.save(False)
                    self.set_origin(None)
                    self.action_url = None
                elif action == 'enable':
                    if not await self.listen():
                        return self.view('error', f'本機 {self.ports["local_port"]} 已被占用，請到上方連接埠卡片改埠後重試。')
                    # Remember this choice even while the owner completes official consent.
                    self.save(True)
                    self.set_origin(origin)
                    code, output = await command(self.cli(), 'funnel', '--bg', '--yes', f'--https={self.ports["public_port"]}', self.target, timeout=20)
                    self.action_url = official_url(output)
                    if code and not self.action_url:
                        self.last_error = 'Funnel 啟用未完成，請確認帳號允許 Funnel，或更新 Tailscale 後重試。'
                return await self.status()
            except (OSError, ValueError, TypeError, AttributeError):
                return self.view('error', '分享操作未完成，請確認 Tailscale 已啟動後重試。')
