"""Experimental 1111 browser bridge; never called while its admin switch is off."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
from urllib.parse import urlsplit

from .job_normalize import _clean_text


_MESSAGES = {
    'blocked': '1111 要求人機驗證或拒絕請求，這次未取得可靠結果。',
    'unsupported': '1111 實驗來源目前無法支援此查詢或頁面格式，未改查全國。',
    'time_limited': '時間不足，此地區未查；可按續查補查。',
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
        def optional_text(value):
            if not isinstance(value, str):
                return None
            text = _clean_text(value)
            return text if text and len(text) <= 500 else None

        location = optional_text(row.get('location'))
        salary = optional_text(row.get('salary'))
        missing = [label for label, value in (('地點', location), ('薪資', salary)) if value is None]
        warning = '1111 為實驗來源，可能因人機驗證受阻'
        if missing:
            warning += '；' + '、'.join(missing) + '未能解析'
        warning += '，請至來源核對。'
        rows.append(dict(source_job_id=row['id'], title=title, company_name=company, company_source_id=corp,
                         url='https://www.1111.com.tw/job/' + row['id'] + '/', location=location, salary_text=salary,
                         summary=_clean_text(row.get('text')), filter_warnings=[warning]))
    return rows


# Conservative admission estimate; a page can spend 30 seconds navigating plus DOM validation.
SINGLE_AREA_SECONDS = 45
# Mirrors source.mjs STARTUP_MS: imports and Node overhead plus the 15s Chrome launch.
STARTUP_SECONDS = 20
# Allow worker JSON output and context shutdown before the bridge's hard timeout.
WORKER_GRACE_SECONDS = 8
MAX_AREAS = 10
MAX_SEARCH_PAGES = 5


def call_seconds(area_count):
    """One browser execution, including startup and graceful shutdown (source.mjs constants)."""
    return STARTUP_SECONDS + SINGLE_AREA_SECONDS * max(1, area_count) + WORKER_GRACE_SECONDS


def search_batch(payload, page, names, previous_totals):
    """Validate each region with the single-page contract; one bad region cannot erase others."""
    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        raise BrowserError('unsupported')
    if 'areas' in payload:
        results = payload['areas']
        expected = names or ['']
        if (not isinstance(results, list) or len(results) != len(expected) or
                any(not isinstance(item, dict) or item.get('area_name') != name
                    for item, name in zip(results, expected))):
            raise BrowserError('unsupported')
    else:
        if len(names) > 1:
            raise BrowserError('unsupported')
        results = [dict(payload, area_name=names[0] if names else '')]
    rows, warnings, failures, next_areas = [], [], [], []
    totals = dict(previous_totals)
    successes = 0
    deferred = []
    for item in results:
        name = item['area_name']
        try:
            if item.get('status') != 'ok':
                status, message = item.get('status'), item.get('message')
                if status not in _MESSAGES or not isinstance(message, str) or not message or len(message) > 500:
                    raise BrowserError('unsupported')
                if status == 'time_limited':
                    deferred.append(name)
                    continue
                raise BrowserError(status, message)
            region_rows = search_rows(item, page)
        except BrowserError as exc:
            failures.append(exc.status)
            warnings.append(f'1111 {name or "全台"}：{exc.message}（未取得結果）')
            continue
        successes += 1
        rows.extend(region_rows)
        totals[name] = item['total']
        if item['has_next']:
            next_areas.append(name)
        warnings.extend(item.get('warnings', []))
    if not successes and not deferred:
        status = 'blocked' if 'blocked' in failures else 'failed' if 'failed' in failures else 'unsupported'
        raise BrowserError(status, '；'.join(warnings))
    # Sources.add_jobs performs source_job_id de-duplication and accounts for duplicates.
    return rows, sum(totals.values()), next_areas, warnings, totals, deferred


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

    async def call(self, arguments, *, timeout=None):
        seconds = call_seconds(len(arguments.get('area_names', [])))
        started = time.monotonic()
        caller_expires = None if timeout is None else started + max(0, timeout)
        wait_seconds = seconds if caller_expires is None else max(0, caller_expires - started)
        executing = False
        try:
            # Queue until the caller's deadline, or one execution cap without a deadline.
            async with asyncio.timeout(wait_seconds) as timer, self.lock:
                acquired = time.monotonic()
                cap = seconds if caller_expires is None else min(seconds, caller_expires - acquired)
                expires = acquired + cap
                timer.reschedule(asyncio.get_running_loop().time() + max(0, cap))
                if cap - WORKER_GRACE_SECONDS < SINGLE_AREA_SECONDS + STARTUP_SECONDS:
                    raise BrowserError('time_limited')
                node = shutil.which('node')
                if not node or not self.script.is_file():
                    raise BrowserError('unsupported', '1111 實驗來源需 Node 20+、Chrome 及 experiments/1111-playwright 的 npm ci 依賴；不會自動安裝。')
                process = None
                executing = True
                starting = asyncio.create_task(asyncio.create_subprocess_exec(node, str(self.script), cwd=self.script.parent,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    **({'start_new_session': True} if os.name != 'nt' else {})))
                cancelled = False
                try:
                    # Startup may already own a child when cancellation arrives; obtain its handle before cleanup.
                    while not starting.done():
                        try:
                            await asyncio.shield(starting)
                        except asyncio.CancelledError:
                            cancelled = True
                    process = starting.result()
                    if cancelled:
                        raise asyncio.CancelledError
                    # Lock wait and Node startup consume the same budget, never a fresh one.
                    worker_input = dict(arguments, budget_ms=max(1, int(
                        (expires - time.monotonic() - WORKER_GRACE_SECONDS) * 1000)))
                    process.stdin.write(json.dumps(worker_input, ensure_ascii=False).encode('utf-8'))
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
                finally:
                    if process is not None and process.returncode is None:
                        cleanup = asyncio.create_task(self._stop(process))
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                continue
                        cleanup.result()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if not executing:
                raise BrowserError('time_limited') from None
            raise BrowserError('failed', '1111 查詢執行時間已到，已停止本次瀏覽器。') from None
        except (OSError, ValueError):
            raise BrowserError('unsupported', '1111 執行環境或回傳格式無法辨識；請檢查 Node、Chrome 及實驗 npm ci 依賴。') from None
