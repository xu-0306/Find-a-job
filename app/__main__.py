import argparse
import getpass
import os
from pathlib import Path
import subprocess
import sys

from .security import password_hash
from .store import Store
from .ports import config_path, read_config


def serve_app(*, native_sharing=False, admin_port=None):
    from .main import create_app
    return create_app(native_sharing=native_sharing, admin_port=admin_port)


def stop_previous_backends():
    python_path = str(Path(sys.executable).resolve()).replace("'", "''")
    script = f"$pythonPath = '{python_path}'; $excluded = @({os.getpid()}, {os.getppid()});\n" + r'''
$ErrorActionPreference = 'Stop'
$modulePattern = '\s+(?:-[bBdEiIOPqsSuvx]+\s+)*-m\s+app\s+serve(?:\s|$)'
$servePattern = '^(?:"[^"]+"|\S+)' + $modulePattern
$absolutePattern = '^"?' + [regex]::Escape($pythonPath) + '"?' + $modulePattern
$processes = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
$roots = @($processes | Where-Object {
    $_.ProcessId -notin $excluded -and $_.CommandLine -match $servePattern -and
    ($_.ExecutablePath -eq $pythonPath -or $_.CommandLine -match $absolutePattern)
} | Select-Object -ExpandProperty ProcessId)
$backends = @($processes | Where-Object {
    $_.ProcessId -notin $excluded -and $_.CommandLine -match $servePattern -and
    ($_.ProcessId -in $roots -or $_.ParentProcessId -in $roots)
})
foreach ($backend in $backends) {
    $running = Get-Process -Id $backend.ProcessId -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "Stopping previous Job Companion backend (PID $($backend.ProcessId))..."
        $running | Stop-Process -ErrorAction Stop
        if (!$running.WaitForExit(5000)) { throw 'Previous backend did not exit.' }
    }
}
exit 0
'''
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description="求職陪伴小幫手本機管理")
    parser.add_argument("command", choices=["init-admin", "serve"])
    parser.add_argument("--port", type=int, help='本次啟動覆蓋設定頁保存的本機後台埠')
    parser.add_argument("--host", choices=['127.0.0.1','0.0.0.0'], default='127.0.0.1', help='0.0.0.0 僅供 loopback 發布的容器')
    parser.add_argument('--restart', action='store_true', help='Windows 啟動前關閉本專案先前的後端')
    args = parser.parse_args()
    if args.restart and (args.command != 'serve' or os.name != 'nt'):
        parser.error('--restart 僅適用於 Windows serve')
    if args.command == 'serve' and args.port is not None and not 0 <= args.port <= 65535:
        parser.error('--port 須介於 0 與 65535')
    if args.command == "init-admin":
        password = getpass.getpass("設定管理員密碼（至少 4 字元）：")
        if len(password) < 4:
            parser.error("密碼至少需要 4 個字元")
        if password != getpass.getpass("再次輸入密碼："):
            parser.error("兩次密碼不同")
        store = Store()
        with store.connect(write=True) as db:
            store.put(db, "admin_password_hash", password_hash(password))
            db.execute("DELETE FROM sessions WHERE role='admin'")
            db.execute("DELETE FROM login_attempts")
        print("管理員密碼已保存為雜湊；既有後台登入已失效。")
    else:
        try:
            ports = read_config(config_path())
        except (OSError, ValueError):
            parser.exit(1, '連接埠設定無法讀取，請修正資料目錄內的 funnel.json 後再啟動。\n')
        port = ports['admin_port'] if args.port is None else args.port
        native_sharing = os.name == 'nt' and args.host == '127.0.0.1'
        if native_sharing and port == ports['local_port']:
            parser.error('本機後台與前台連接埠不能相同')
        # Native start.cmd needs the same explicit public origin as the HTTPS proxy.
        env_file = Path(__file__).resolve().parents[1] / '.env'
        if 'JOB_COMPANION_PUBLIC_ORIGIN' not in os.environ and env_file.is_file():
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                key, separator, value = line.partition('=')
                if separator and key.strip() == 'JOB_COMPANION_PUBLIC_ORIGIN':
                    os.environ[key.strip()] = value.strip()
        import uvicorn
        from functools import partial
        if args.restart:
            try:
                stop_previous_backends()
            except (OSError, subprocess.SubprocessError):
                parser.exit(1, '無法關閉先前的後端，啟動已停止；請查看上方錯誤。\n')
        # Access-link tokens must never enter HTTP access logs; do not trust proxy peers.
        print(f'Admin URL: http://localhost:{port}/admin', flush=True)
        uvicorn.run(partial(serve_app, native_sharing=native_sharing, admin_port=port), factory=True, host=args.host, port=port,
                    workers=1, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
