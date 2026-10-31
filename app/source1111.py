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
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_AREAS = 10
MAX_SEARCH_PAGES = 5


def call_seconds(area_count):
    """One browser execution, including startup and graceful shutdown (source.mjs constants)."""
    return STARTUP_SECONDS + SINGLE_AREA_SECONDS * max(1, area_count) + WORKER_GRACE_SECONDS


def search_batch(payload, page, names, previous_totals):
    """Return independently validated rows and state for every requested area."""
    expected = names or ['']
    if not isinstance(payload, dict):
        raise BrowserError('unsupported')
    if payload.get('status') != 'ok':
        status = payload.get('status')
        if status not in _MESSAGES:
            status = 'unsupported'
        message = payload.get('message')
        if not isinstance(message, str) or not message or len(message) > 500:
            message = _MESSAGES[status]
        return {'areas': [dict(area_name=name, rows=[], status='not_queried' if status == 'time_limited' else status,
            total_declared=previous_totals.get(name), has_next=status != 'unsupported', message=message, warnings=[])
            for name in expected]}
    if 'areas' in payload:
        results = payload['areas']
        if (not isinstance(results, list) or len(results) != len(expected) or
                any(not isinstance(item, dict) or item.get('area_name') != name
                    for item, name in zip(results, expected))):
            raise BrowserError('unsupported')
    else:
        if len(names) > 1:
            raise BrowserError('unsupported')
        results = [dict(payload, area_name=expected[0])]

    areas = []
    for item, name in zip(results, expected):
        status = item.get('status')
        if status != 'ok':
            if status not in _MESSAGES or not isinstance(item.get('message'), str) or not item['message'] or len(item['message']) > 500:
                status, message = 'unsupported', _MESSAGES['unsupported']
            else:
                message = item['message']
            areas.append(dict(area_name=name, rows=[], status='not_queried' if status == 'time_limited' else status,
                total_declared=previous_totals.get(name), has_next=status != 'unsupported', message=message, warnings=[]))
            continue
        try:
            rows = search_rows(item, page)
        except BrowserError as exc:
            areas.append(dict(area_name=name, rows=[], status=exc.status,
                total_declared=previous_totals.get(name), has_next=True, message=exc.message, warnings=[]))
            continue
        warnings = item.get('warnings', [])
        areas.append(dict(area_name=name, rows=rows,
            status='empty' if item['total'] == 0 and not rows else 'success',
            total_declared=item['total'], has_next=item['has_next'], message=None, warnings=warnings))
    return {'areas': areas}


IDLE_SECONDS = 600


class Browser1111:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.script = Path(__file__).resolve().parents[1] / 'experiments' / '1111-playwright' / 'source.mjs'
        self.process = None
        self._request_id = 0
        self._idle_task = None
        self._idle_closing = False
        self._closing = False

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

    def _cancel_idle(self):
        if self._idle_task and not self._idle_task.done() and not self._idle_closing:
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle(self):
        self._cancel_idle()
        self._idle_task = asyncio.create_task(self._idle_close())

    async def _idle_close(self):
        try:
            await asyncio.sleep(IDLE_SECONDS)
            async with self.lock:
                # Shutdown owns its detached process until wait or forced cleanup completes.
                self._idle_closing = True
                if self.process is None:
                    return
                await self._graceful_close_locked()
        except asyncio.CancelledError:
            return
        finally:
            self._idle_closing = False

    async def _graceful_close_locked(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.returncode is None and process.stdin:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), WORKER_GRACE_SECONDS)
            elif process.returncode is None:
                await asyncio.wait_for(process.wait(), WORKER_GRACE_SECONDS)
        except (asyncio.TimeoutError, OSError):
            await self._stop(process)

    async def close(self):
        """Close the shared worker; safe to call repeatedly."""
        self._closing = True
        self._cancel_idle()
        try:
            await asyncio.wait_for(self.lock.acquire(), WORKER_GRACE_SECONDS)
        except TimeoutError:
            await self._kill_current(self.process)
            return
        try:
            self._cancel_idle()
            await self._graceful_close_locked()
        finally:
            self.lock.release()

    async def _ensure_process(self):
        if self.process is not None and self.process.returncode is None:
            return self.process
        self.process = None
        node = shutil.which('node')
        if not node or not self.script.is_file():
            raise BrowserError('unsupported', '1111 實驗來源需 Node 20+、Chrome 及 experiments/1111-playwright 的 npm ci 依賴；不會自動安裝。')
        self.process = await asyncio.create_subprocess_exec(node, str(self.script), '--serve', cwd=self.script.parent,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_REPLY_BYTES,
            **({'start_new_session': True} if os.name != 'nt' else {}))
        return self.process

    async def _kill_current(self, process):
        if self.process is process:
            self.process = None
        if process is not None and process.returncode is None:
            await self._stop(process)

    async def call(self, arguments, *, timeout=None):
        seconds = call_seconds(len(arguments.get('area_names', [])))
        started = time.monotonic()
        caller_expires = None if timeout is None else started + max(0, timeout)
        wait_seconds = seconds if caller_expires is None else max(0, caller_expires - started)
        executing = False
        try:
            async with asyncio.timeout(wait_seconds) as timer:
                self._cancel_idle()
                async with self.lock:
                    self._closing = False
                    try:
                        acquired = time.monotonic()
                        cap = seconds if caller_expires is None else min(seconds, caller_expires - acquired)
                        expires = acquired + cap
                        timer.reschedule(asyncio.get_running_loop().time() + max(0, cap))
                        if cap - WORKER_GRACE_SECONDS < SINGLE_AREA_SECONDS + STARTUP_SECONDS:
                            raise BrowserError('time_limited')
                        process = None
                        executing = True
                        public_result_error = False
                        starting = asyncio.create_task(self._ensure_process())
                        try:
                            cancelled = False
                            while not starting.done():
                                try:
                                    await asyncio.shield(starting)
                                except asyncio.CancelledError:
                                    cancelled = True
                            process = starting.result()
                            if cancelled:
                                raise asyncio.CancelledError
                            if self._closing:
                                raise BrowserError()
                            self._request_id += 1
                            request_id = str(self._request_id)
                            worker_input = dict(arguments, budget_ms=max(1, int(
                                (expires - time.monotonic() - WORKER_GRACE_SECONDS) * 1000)))
                            process.stdin.write((json.dumps({'id': request_id, 'input': worker_input}, ensure_ascii=False) + '\n').encode('utf-8'))
                            await process.stdin.drain()
                            line = await process.stdout.readline()
                            if not line or not line.endswith(b'\n'):
                                raise BrowserError('unsupported')
                            payload = json.loads(line)
                            if (not isinstance(payload, dict) or payload.get('id') != request_id
                                    or not isinstance(payload.get('result'), dict)):
                                raise BrowserError('unsupported')
                            if process.returncode is not None:
                                raise BrowserError()
                            result = payload['result']
                            if result.get('status') != 'ok':
                                status, message = result.get('status'), result.get('message')
                                if status not in _MESSAGES or not isinstance(message, str) or not message or len(message) > 500:
                                    raise BrowserError('unsupported')
                                public_result_error = True
                                raise BrowserError(status, message)
                            return result
                        except asyncio.CancelledError:
                            await asyncio.shield(self._kill_current(process))
                            raise
                        except BrowserError:
                            if not public_result_error:
                                await self._kill_current(process)
                            raise
                        except (OSError, ValueError):
                            await self._kill_current(process)
                            raise
                    finally:
                        if self.process is not None and self.process.returncode is None and not self._closing:
                            self._arm_idle()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if not executing:
                raise BrowserError('time_limited') from None
            raise BrowserError('failed', '1111 查詢執行時間已到，已停止本次瀏覽器。') from None
        except BrowserError:
            raise
        except (OSError, ValueError):
            raise BrowserError('unsupported', '1111 執行環境或回傳格式無法辨識；請檢查 Node、Chrome 及實驗 npm ci 依賴。') from None
