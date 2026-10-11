"""Experimental 1111 browser bridge; never called while its admin switch is off."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
from urllib.parse import urlsplit

from .job_normalize import _clean_text


_MESSAGES = {
    'blocked': '1111 要求人機驗證或拒絕請求，這次未取得可靠結果。',
    'unsupported': '1111 實驗來源目前無法支援此查詢或頁面格式，未改查全國。',
    'failed': '1111 實驗來源暫時無法查詢，請稍後再試。',
}


class BrowserError(Exception):
    def __init__(self, status='failed', message=None):
        self.status = status if status in _MESSAGES else 'failed'
        self.message = message or _MESSAGES[self.status]
        super().__init__(self.message)


def _source_path(url, kind):
    try:
        parsed = urlsplit(str(url))
        if (parsed.scheme != 'https' or parsed.hostname != 'www.1111.com.tw' or parsed.username or
                parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            return None
        match = re.fullmatch('/' + kind + r'/([0-9]+)/?', parsed.path)
        return match[1] if match else None
    except (TypeError, ValueError):
        return None


def job_url(url, ident):
    return isinstance(ident, str) and bool(re.fullmatch(r'[0-9]+', ident)) and _source_path(url, 'job') == ident


def company_id(url):
    return _source_path(url, 'corp')


def search_rows(payload, page):
    """Accept complete page metadata and safe public identity fields only."""
    if (not isinstance(payload, dict) or payload.get('status') != 'ok' or
            type(payload.get('page')) is not int or payload['page'] != page or
            type(payload.get('page_size')) is not int or payload['page_size'] < 1 or
            type(payload.get('total')) is not int or payload['total'] < 0 or
            type(payload.get('has_next')) is not bool or not isinstance(payload.get('jobs'), list) or
            not isinstance(payload.get('warnings', []), list) or
            any(not isinstance(w, str) for w in payload.get('warnings', []))):
        raise BrowserError('unsupported')
    expected = min(payload['page_size'], max(0, payload['total'] - (page - 1) * payload['page_size']))
    if len(payload['jobs']) != expected or payload['has_next'] != (page * payload['page_size'] < payload['total']):
        raise BrowserError('unsupported', '1111 回傳頁面不完整或頁碼不符，未當成零筆結果。')
    rows, seen = [], set()
    for row in payload['jobs']:
        if not isinstance(row, dict) or not job_url(row.get('url'), row.get('id')) or row['id'] in seen:
            raise BrowserError('unsupported', '1111 職缺識別或網址不符，已停止讀取。')
        seen.add(row['id'])
        try:
            title, company = _clean_text(row.get('title')), _clean_text(row.get('company'))
        except ValueError:
            raise BrowserError('unsupported') from None
        if not title or not company:
            raise BrowserError('unsupported')
        corp = company_id(row.get('companyUrl'))
        if not corp:
            raise BrowserError('unsupported', '1111 職缺缺少可靠的公司識別資料。')
        rows.append(dict(source_job_id=row['id'], title=title, company_name=company, company_source_id=corp,
                         url='https://www.1111.com.tw/job/' + row['id'] + '/', location=None, salary_text=None,
                         summary=_clean_text(row.get('text')),
                         filter_warnings=['1111 為實驗來源，可能因人機驗證受阻；地點與薪資尚未可靠解析，請至來源核對。']))
    return rows


class Browser1111:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.script = Path(__file__).resolve().parents[1] / 'experiments' / '1111-playwright' / 'source.mjs'

    async def _stop(self, process):
        # Terminate the owned Node subtree before cancellation can leave Chrome behind.
        if os.name == 'nt':
            killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def call(self, arguments):
        async with self.lock:
            node = shutil.which('node')
            if not node or not self.script.is_file():
                raise BrowserError('unsupported', '1111 實驗來源需 Node 20+、Chrome 及 experiments/1111-playwright 的 npm ci 依賴；不會自動安裝。')
            process = None
            try:
                process = await asyncio.create_subprocess_exec(node, str(self.script), cwd=self.script.parent,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    **({'start_new_session': True} if os.name != 'nt' else {}))
                async with asyncio.timeout(60):
                    process.stdin.write(json.dumps(arguments, ensure_ascii=False).encode('utf-8'))
                    await process.stdin.drain()
                    process.stdin.close()
                    output = bytearray()
                    while chunk := await process.stdout.read(65536):
                        output.extend(chunk)
                        if len(output) > 2 * 1024 * 1024:
                            raise BrowserError('unsupported')
                    await process.wait()
                payload = json.loads(output)
                if not isinstance(payload, dict):
                    raise BrowserError('unsupported')
                if payload.get('status') != 'ok':
                    status = payload.get('status')
                    message = payload.get('message')
                    # The worker emits only fixed public diagnostics, never raw page or browser errors.
                    if status not in _MESSAGES or not isinstance(message, str) or len(message) > 500:
                        raise BrowserError('unsupported')
                    raise BrowserError(status, message)
                if process.returncode:
                    raise BrowserError()
                return payload
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise BrowserError('failed', '1111 查詢等候時間已到，已停止本次瀏覽器。') from None
            except (OSError, ValueError):
                raise BrowserError('unsupported', '1111 執行環境或回傳格式無法辨識；請檢查 Node、Chrome 及實驗 npm ci 依賴。') from None
            finally:
                if process is not None and process.returncode is None:
                    cleanup = asyncio.create_task(self._stop(process))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
