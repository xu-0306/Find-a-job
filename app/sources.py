"""Bounded source requests; parsing and persistence remain separate."""
import asyncio
import json
import re
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from pydantic import Field, StrictInt

from .job_normalize import company_excluded, company_key, normalize_jobs
from .source518 import parse_search_response, parse_detail_response
from .source1111 import Browser1111, BrowserError, search_rows, job_url, company_id
from .store import Criteria, Input, Job


def enabled_sources(services):
    """104 is always on; 518 defaults on and experimental 1111 defaults off."""
    return (['104'] + (['518'] if services.get('source_518_enabled') is not False else [])
            + (['1111'] if services.get('source_1111_enabled') is True else []))


_SOURCE_MENTION = re.compile(r'(?<!\d)(?:104|518|1111)(?!\d)')


def requested_sources(arguments, enabled, user_text, continuation=False):
    """Sources a search asks for, before disabled ones are dropped.

    A new search queries every enabled source unless this turn's user message names a source ("只查 104"、"不要 1111"):
    models narrowed the list on their own, which silently skipped sources the admin had turned on. A continuation
    keeps its saved sources, since its cursor has no position for a newly added one.
    """
    requested = list(arguments.sources)
    if not continuation and ('sources' not in arguments.model_fields_set or not _SOURCE_MENTION.search(user_text or '')):
        requested += [source for source in enabled if source not in requested]
    return requested


class SearchInput(Input):
    criteria: Criteria
    sources: list[str] = Field(default_factory=lambda: ['104', '518'], min_length=1, max_length=3,
        description='通常省略：一律查詢後台已啟用的全部來源（104，以及已啟用的 518、1111）。只有使用者這則訊息明確指定或排除某個人力銀行時才填，且只有那時才會限縮。1111 為實驗來源，可能因人機驗證受阻。')
    limit: StrictInt = Field(default=20, ge=1, le=20)
    search_id: str | None = Field(default=None, min_length=1, max_length=100)
    exclude_companies: list[str] = Field(default_factory=list, max_length=20,
        description='使用者要求排除的公司。同時列出使用者說法與搜尋結果中出現的公司全名（例如簡稱與全名各一項）；比對忽略全半形、空白及「股份有限公司」等字樣，任一項包含於公司名稱即排除。排除會套用到這段對話之後的所有搜尋與續查。')

    def model_post_init(self, context):
        if any(x not in ('104', '518', '1111') for x in self.sources) or len(set(self.sources)) != len(self.sources):
            raise ValueError('來源不符或重複')
        if any(not name or len(name) > 80 or len(company_key(name)) < 2 for name in self.exclude_companies):
            raise ValueError('排除公司名稱須為2～80字')
        self.exclude_companies = list(dict.fromkeys(self.exclude_companies))


class DetailInput(Input):
    job_id: str | None = Field(default=None, min_length=1, max_length=100,
        description='已保存職缺的 job_id；與 url 擇一。')
    url: str | None = Field(default=None, min_length=1, max_length=2048,
        description='使用者提供的 HTTPS 104 職缺網址 https://www.104.com.tw/job/<id>，可帶查詢參數；與 job_id 擇一。')

    def model_post_init(self, context):
        if (self.job_id is None) == (self.url is None):
            raise ValueError('job_id 與 url 須擇一提供')
        if self.url is not None and (not safe_url(self.url, 'www.104.com.tw') or
                not re.fullmatch('/job/[A-Za-z0-9]+', urlsplit(self.url).path)):
            raise ValueError('只接受合法的 HTTPS 104 職缺網址')


class DailySearchInput(SearchInput):
    """Internal daily entry; the LLM continues to use SearchInput's 20-item cap."""
    limit: StrictInt = Field(default=100, ge=1, le=100)


class SourceError(Exception):
    def __init__(self, status='failed', message='來源暫時無法查詢，請稍後再試。'):
        self.status, self.message = status, message


def safe_url(url, host, path=None):
    try:
        u = urlsplit(str(url))
        return (u.scheme == 'https' and u.hostname == host and not u.username and not u.password
                and u.port in (None, 443) and not u.fragment and (path is None or u.path == path))
    except ValueError:
        return False


def area_catalog():
    from job104_mcp.codes import load_area
    # Package 0.1.8's published tree includes city and district nodes.
    return [{'code': code, 'name': name} for code, name in load_area()._entries]


def region_map(body):
    """518's official JS contains two finite code/name dictionaries; never eval JS."""
    def dictionary(key, digits):
        match = re.search(r'["\']?' + key + r'["\']?\s*:\s*(\{[^{}]+\})', body)
        if not match: raise SourceError('unsupported', '518 地區資料格式已變更。')
        text = re.sub(r'(?<=[{,])\s*(\d+)\s*:', r'"\1":', match[1])
        try: values = json.loads(text)
        except ValueError: raise SourceError('unsupported', '518 地區資料格式已變更。') from None
        if not values or any(not re.fullmatch(r'\d{' + str(digits) + r'}', str(k)) or not isinstance(v, str) or not v for k, v in values.items()):
            raise SourceError('unsupported', '518 地區資料不完整。')
        return values
    parents, children = dictionary('times_location', 7), dictionary('sub_location', 10)
    norm = lambda value: unicodedata.normalize('NFKC', value).replace('臺', '台').strip()
    mapping = {}
    for code, city in parents.items(): mapping.setdefault(norm(city), set()).add(code)
    for code, district in children.items():
        if code[:7] not in parents: raise SourceError('unsupported', '518 地區父子關係不符。')
        full = norm(parents[code[:7]]) + norm(district)
        mapping.setdefault(full, set()).add(code)
    output = {}
    for item in area_catalog():
        name = norm(item['name'])
        codes = mapping.get(name)
        if not codes and name.endswith('縣市'):
            county, city = mapping.get(name[:-2] + '縣'), mapping.get(name[:-2] + '市')
            if county and city: codes = county | city
        if not codes:
            # Provider-local spelling variation: absent administrative suffix, within full city context.
            stripped = re.sub(r'[區鄉鎮市]$', '', name)
            matches = [value for full, value in mapping.items() if re.sub(r'[區鄉鎮市]$', '', full) == stripped]
            if len(matches) == 1: codes = matches[0]
        if codes and (len(codes) == 1 or name in {norm(x) for x in parents.values()} or name.endswith('縣市')):
            output[item['name']] = ','.join(sorted(codes))
    return output


def extract_mcp(result):
    if isinstance(result, tuple):
        return result[1] if len(result) > 1 and isinstance(result[1], dict) else extract_mcp(result[0])
    if isinstance(result, dict):
        return result.get('structuredContent', result)
    if isinstance(result, list):
        blocks = [getattr(x, 'text', None) if not isinstance(x, dict) else x.get('text') for x in result]
        return json.loads(next(x for x in blocks if isinstance(x, str)))
    if isinstance(result, str):
        return json.loads(result)
    raise SourceError()


_mcp_lock = threading.Lock()
_bounded_client = None


def mcp_once(name, arguments, cancelled):
    """Same-process MCP; its HTTP client is replaced once, never requests.get."""
    global _bounded_client
    from job104_mcp import server
    from job104_mcp.client import Client, Client104Error
    from curl_cffi import requests

    while not _mcp_lock.acquire(timeout=.1):
        if cancelled.is_set():
            raise SourceError()
    try:
        if _bounded_client is None:
            class BoundedClient(Client):
                def _get(self, url, params=None):
                    if not safe_url(url, 'www.104.com.tw'):
                        raise Client104Error('Unsafe URL')
                    for attempt in range(2):
                        if self.cancelled.wait(max(0, 1 - (time.monotonic() - self._last_request))):
                            raise Client104Error('Cancelled')
                        self._last_request = time.monotonic()
                        try:
                            response = requests.get(url, params=params, timeout=30, impersonate='chrome',
                                                    allow_redirects=False, headers={'Referer': 'https://www.104.com.tw/jobs/search/'})
                        except Exception:
                            if attempt or self.cancelled.is_set():
                                raise Client104Error('Network unavailable') from None
                            continue
                        self.http_status = response.status_code
                        if response.status_code == 200:
                            return response
                        if attempt or response.status_code not in (429, 500, 502, 503, 504):
                            raise Client104Error('HTTP unavailable')
                    raise Client104Error('HTTP unavailable')

                def _parse_json(self, response):
                    raw = super()._parse_json(response)
                    # MCP's tolerant mapper turns changed JSON into empty; reject that here.
                    if not isinstance(raw, dict) or not isinstance(raw.get('data'), (list, dict)):
                        raise Client104Error('Changed schema')
                    if isinstance(raw['data'], list) and not isinstance(raw.get('metadata', {}).get('pagination'), dict):
                        raise Client104Error('Changed pagination')
                    self.raw = raw
                    return raw
            _bounded_client = BoundedClient()
            server._client = _bounded_client
        _bounded_client.cancelled = cancelled
        _bounded_client.http_status = None
        _bounded_client.raw = None
        if cancelled.is_set():
            raise SourceError()
        result = extract_mcp(asyncio.run(server.mcp.call_tool(name, arguments)))
        if not isinstance(result, dict) or result.get('error'):
            raise SourceError('blocked' if _bounded_client.http_status in (401, 403, 429) else 'failed')
        return result, _bounded_client.raw
    finally:
        _mcp_lock.release()


_CONTINUATION_VERSION = 1
_MAX_NEW_PAGES = 5


def _criteria_state(arguments):
    return {
        'keyword': arguments.criteria.keyword,
        'area_codes': list(arguments.criteria.area_codes),
        'min_monthly_salary': arguments.criteria.min_monthly_salary,
    }


def _page_size(arguments):
    # DailySearchInput keeps its existing 100-result cap over five 20-row pages.
    return min(20, arguments.limit)


def _job_state(job):
    return job.model_dump(mode='json')


def _cursor_error(message):
    raise ValueError(message)


def _load_continuation(arguments, continuation):
    if continuation is None:
        return None
    if isinstance(continuation, str):
        try:
            state = json.loads(continuation)
        except (TypeError, ValueError):
            _cursor_error('續查位置無效，請從原搜尋重新開始。')
    elif isinstance(continuation, dict):
        state = continuation
    else:
        _cursor_error('續查位置無效，請從原搜尋重新開始。')
    if not isinstance(state, dict) or state.get('version') != _CONTINUATION_VERSION:
        _cursor_error('續查位置版本不支援，請從原搜尋重新開始。')
    if state.get('criteria') != _criteria_state(arguments):
        _cursor_error('續查條件已變更，請從新搜尋開始。')
    saved_sources = state.get('sources')
    # A continuation may query fewer sources than were saved (a source disabled later), never a source it lacks state for.
    if not isinstance(saved_sources, dict) or not set(arguments.sources) <= set(saved_sources):
        _cursor_error('續查來源已變更，請從新搜尋開始。')
    request_page_size = _page_size(arguments)
    for source in arguments.sources:
        item = saved_sources.get(source)
        if not isinstance(item, dict) or item.get('page_size') != request_page_size:
            _cursor_error('續查每頁筆數已變更，請沿用原搜尋筆數。')
        if not isinstance(item.get('seen_ids', []), list) or not all(isinstance(x, str) for x in item.get('seen_ids', [])):
            _cursor_error('續查已取得的職缺識別資料無效。')
        if not isinstance(item.get('page_signatures', []), list) or not all(isinstance(x, str) for x in item.get('page_signatures', [])):
            _cursor_error('續查頁面內容紀錄無效，請從原搜尋重新開始。')
        if not isinstance(item.get('parser_warnings', []), list) or not all(isinstance(x, str) for x in item.get('parser_warnings', [])):
            _cursor_error('續查來源診斷資料無效，請從原搜尋重新開始。')
        if not isinstance(item.get('remaining', []), list):
            _cursor_error('續查尚未顯示的職缺資料無效。')
        try:
            item['next_page'] = int(item.get('next_page', 1))
            item['pages'] = int(item.get('pages', 0))
        except (TypeError, ValueError):
            _cursor_error('續查頁碼無效，請從原搜尋重新開始。')
        if item['next_page'] < 1 or item['pages'] < 0:
            _cursor_error('續查頁碼無效，請從原搜尋重新開始。')
        if not isinstance(item.get('visited', []), list) or not all(isinstance(x, str) for x in item.get('visited', [])):
            _cursor_error('續查頁面紀錄無效，請從原搜尋重新開始。')
    return state


def _cursor_result(arguments, source_state):
    state = {
        'version': _CONTINUATION_VERSION,
        'criteria': _criteria_state(arguments),
        'sources': source_state,
    }
    return json.dumps(state, ensure_ascii=False, separators=(',', ':'))


def _source_job_keys(job):
    return {job.source_job_id, f'{job.source}:{job.source_job_id}'}


def _valid_518_cursor(url, page, keyword, areas, reference_url=None):
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    except (TypeError, ValueError):
        return False
    if not safe_url(url, 'www.518.com.tw', f'/job-index-P-{page}.html'):
        return False
    if set(values) - {'ad', 'aa', 'ai'} or values.get('ad') != [keyword]:
        return False
    if areas:
        if values.get('aa') != [areas]:
            return False
    elif values.get('aa') not in (None, ['']):
        return False
    # The live source adds a one-digit ``ai`` marker to otherwise equivalent
    # next links. Keep its shape narrow and reject a changed marker after it
    # first appears; an omitted marker remains semantically equivalent.
    if 'ai' in values and (len(values['ai']) != 1 or not re.fullmatch(r'\d', values['ai'][0])):
        return False
    if reference_url:
        try:
            previous = parse_qs(urlsplit(reference_url).query, keep_blank_values=True)
        except (TypeError, ValueError):
            return False
        if 'ai' in previous and 'ai' in values and values['ai'] != previous['ai']:
            return False
        if 'ai' in values and 'ai' not in previous:
            previous_match = re.fullmatch(r'/job-index-P-(\d+)\.html', urlsplit(reference_url).path)
            if not previous_match or previous_match.group(1) != '1':
                return False
    return True


class Sources:
    def __init__(self, transport=None, mcp_call=None, interval=1, browser1111=None):
        self.transport, self.mcp_call, self.interval = transport, mcp_call, interval
        self.locks, self.last = {}, {}
        self.regions518 = None
        self.browser1111 = browser1111 or Browser1111()

    async def request(self, method, url, *, host=None, max_retries=1, **kwargs):
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError('max_retries must be a non-negative integer')
        host = host or urlsplit(url).hostname
        if not safe_url(url, host):
            raise SourceError('blocked', '來源網址不符，已停止請求。')
        path = urlsplit(url).path
        lock = self.locks.setdefault(host, asyncio.Lock())
        if self.transport is not None:
            client_context = httpx.AsyncClient(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False)
            network_errors = (httpx.RequestError, TimeoutError)
        else:
            from curl_cffi.requests import AsyncSession
            from curl_cffi.requests.exceptions import RequestException
            # Reuse job104's installed TLS-verifying transport; no certificate bypass.
            client_context = AsyncSession(timeout=30, allow_redirects=False, trust_env=False, verify=True, impersonate='chrome')
            network_errors = (RequestException, TimeoutError)
        async with lock, client_context as client:
            retries, hops = 0, 0
            while True:
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last.get(host, 0))))
                self.last[host] = time.monotonic()
                try:
                    async with asyncio.timeout(30):
                        response = await client.request(method, url, **kwargs)
                except network_errors:
                    if retries >= max_retries:
                        raise SourceError() from None
                    retries += 1
                    continue
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(str(response.url), response.headers.get('location', ''))
                    if hops >= 3 or not safe_url(target, host, path) or urlsplit(target).query != urlsplit(str(response.url)).query:
                        raise SourceError('blocked', '來源重新導向改變網址或篩選，已停止請求。')
                    hops += 1; url = target
                    kwargs.pop('params', None)
                    continue
                if response.status_code in (429, 500, 502, 503, 504) and retries < max_retries:
                    retries += 1
                    continue
                if response.status_code in (401, 403, 429):
                    raise SourceError('blocked', '來源暫時拒絕請求或要求驗證。')
                if response.status_code == 422 and host == 'api.search.brave.com':
                    try: invalid = response.json().get('error', {}).get('code') == 'SUBSCRIPTION_TOKEN_INVALID'
                    except (ValueError, AttributeError): invalid = False
                    if invalid: raise SourceError('failed', 'Brave 金鑰無效，請在後台重新設定。')
                if not 200 <= response.status_code < 300:
                    raise SourceError()
                return response

    async def call104(self, name, arguments):
        if self.mcp_call:
            return await self.mcp_call(name, arguments)
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(mcp_once, name, arguments, cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # Keep ownership until the bounded in-flight synchronous request exits.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise

    async def search(self, arguments, *, continuation=None, exclude_ids=None, exclude_companies=None):
        result = {
            'items': [], 'source_results': [], 'filter_warnings': [],
            'new_count': 0, 'duplicate_count': 0, 'excluded_count': 0,
            'can_continue': False, 'continuation': None,
        }
        try:
            cursor = _load_continuation(arguments, continuation)
        except ValueError as exc:
            result['source_results'] = [dict(
                source=source, status='unsupported', truncated=False, quota_limited=False,
                pages=0, pages_fetched=0, returned_count=0, new_count=0,
                duplicate_count=0, excluded_count=0, total_declared=None,
                parser_warning_count=0, parser_warnings=[], message=str(exc),
                baseline_eligible=False,
            ) for source in arguments.sources]
            return result
        excluded = ({str(exclude_ids)} if isinstance(exclude_ids, str) else
                    {str(value) for value in (exclude_ids or []) if value is not None})
        company_keys = [key for key in map(company_key, exclude_companies or []) if len(key) >= 2]

        def is_excluded(job):
            return bool(_source_job_keys(job) & excluded) or company_excluded(job.company_name, company_keys)

        saved_sources = cursor.get('sources', {}) if cursor else {}
        cursor_sources = {}

        for source in arguments.sources:
            diagnostic = dict(
                source=source, status='failed', truncated=False, quota_limited=False,
                pages=0, pages_fetched=0, returned_count=0, new_count=0,
                duplicate_count=0, excluded_count=0, total_declared=None,
                parser_warning_count=0, parser_warnings=[], message=None,
                baseline_eligible=False,
            )
            rows, remaining, seen_ids = [], [], set()
            reliable = True
            source_new = source_duplicates = source_excluded = 0
            pages_fetched = 0
            parser_warnings = []
            page_state = dict(saved_sources.get(source, {})) if cursor else {
                'next_page': 1, 'next_url': None, 'page_size': _page_size(arguments),
                'remaining': [], 'seen_ids': [], 'pages': 0, 'visited': [], 'page_signatures': [], 'has_next': True,
            }
            page_state.setdefault('next_page', 1)
            page_state.setdefault('next_url', None)
            page_state.setdefault('page_size', _page_size(arguments))
            page_state.setdefault('remaining', [])
            page_state.setdefault('seen_ids', [])
            page_state.setdefault('pages', 0)
            page_state.setdefault('visited', [])
            page_state.setdefault('page_signatures', [])
            page_state.setdefault('has_next', True)
            page_state.setdefault('total_declared', None)

            def warning(message):
                if message and message not in parser_warnings:
                    parser_warnings.append(message)

            try:
                names = []
                if arguments.criteria.area_codes:
                    table = {x['code']: x['name'] for x in area_catalog()}
                    if any(code not in table for code in arguments.criteria.area_codes):
                        raise SourceError('unsupported', '地區代碼無法辨識，請重新選擇地區。')
                    names = [table[x] for x in arguments.criteria.area_codes]
                if source == '518' and names:
                    if self.regions518 is None:
                        response = await self.request('GET', 'https://statics.518.com.tw/dist/js/common/menu/location.js')
                        self.regions518 = region_map(response.text)
                    if self.regions518 is None or any(name not in self.regions518 for name in names):
                        raise SourceError('unsupported', '518 尚無此地區的可靠對應，未改查全國。')
                    areas = ','.join(dict.fromkeys(code for name in names for code in self.regions518[name].split(',')))
                else:
                    areas = ''
                keyword = arguments.criteria.keyword
                if source == '518' and not page_state['next_url'] and page_state['next_page'] == 1:
                    page_state['next_url'] = 'https://www.518.com.tw/job-index-P-1.html?' + urlencode({'ad': keyword, 'aa': areas})
                # 只有真的會發請求（has_next）才驗證游標；已耗盡的來源沒有下一頁網址。
                if source == '518' and page_state['has_next'] and not _valid_518_cursor(page_state['next_url'], page_state['next_page'], keyword, areas):
                    raise SourceError('blocked', '續查網址不符合原始搜尋條件，已停止請求。')
                if source == '104':
                    page_state['next_url'] = None

                for raw_job in page_state['remaining']:
                    try:
                        remaining.append(Job.model_validate(raw_job))
                    except Exception:
                        raise SourceError('unsupported', '續查尚未顯示的職缺資料無效，請從原搜尋重新開始。') from None
                seen_ids.update(page_state['seen_ids'])
                page_state['visited'] = list(dict.fromkeys(page_state['visited']))
                parser_warnings.extend(page_state.get('parser_warnings', []))
                if parser_warnings:
                    reliable = False
                diagnostic['total_declared'] = page_state.get('total_declared')

                def consume_pending():
                    nonlocal remaining, source_new, source_duplicates, source_excluded
                    pending_ids = {job.source_job_id for job in remaining}
                    pending = []
                    for job in remaining:
                        is_pending = job.source_job_id in pending_ids
                        pending_ids.discard(job.source_job_id)
                        if len(rows) >= arguments.limit:
                            pending.append(job)
                            pending_ids.add(job.source_job_id)
                            continue
                        if job.source_job_id in seen_ids and not is_pending:
                            source_duplicates += 1
                        elif is_excluded(job):
                            seen_ids.add(job.source_job_id)
                            source_excluded += 1
                        else:
                            seen_ids.add(job.source_job_id)
                            rows.append(job)
                            source_new += 1
                    remaining = pending

                def add_jobs(jobs):
                    nonlocal remaining, source_new, source_duplicates, source_excluded
                    queued = {job.source_job_id for job in remaining}
                    for job in jobs:
                        if job.source_job_id in seen_ids or job.source_job_id in queued:
                            source_duplicates += 1
                            continue
                        if is_excluded(job):
                            seen_ids.add(job.source_job_id)
                            source_excluded += 1
                            continue
                        if job.summary and len(job.summary) > 2000:
                            job.summary = job.summary[:2000]
                            job.filter_warnings.append('摘要較長，僅顯示前2000字；完整內容請至來源查看。')
                        if arguments.criteria.min_monthly_salary is not None:
                            job.filter_warnings = list(dict.fromkeys(job.filter_warnings + ['來源未保證最低月薪篩選，請核對薪資與計薪期間。']))
                        if len(rows) < arguments.limit:
                            seen_ids.add(job.source_job_id)
                            rows.append(job)
                            source_new += 1
                        else:
                            seen_ids.add(job.source_job_id)
                            remaining.append(job)
                            queued.add(job.source_job_id)

                consume_pending()
                while len(rows) < arguments.limit and (remaining or page_state['has_next']):
                    if remaining:
                        consume_pending()
                        continue
                    if pages_fetched >= _MAX_NEW_PAGES:
                        diagnostic['quota_limited'] = True
                        break
                    page = page_state['next_page']
                    page_cursor = str(page) if source in ('104', '1111') else page_state['next_url']
                    if page_cursor in page_state['visited']:
                        warning('來源下一頁位置重複，已停止續查以避免重複請求。')
                        reliable = False
                        page_state['has_next'] = False
                        break

                    if source == '104':
                        payload, raw = await self.call104('search_jobs', dict(
                            keyword=keyword, area=names, page=page,
                            page_size=_page_size(arguments), salary_min=arguments.criteria.min_monthly_salary,
                        ))
                        if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list) or type(payload.get('total')) is not int or payload['total'] < 0:
                            raise SourceError()
                        if not payload['jobs'] and payload['total'] > 0:
                            raise SourceError()
                        diagnostic['total_declared'] = payload['total']
                        page_state['total_declared'] = payload['total']
                        page_rows = []
                        raw_data = (raw or {}).get('data', []) if isinstance(raw, dict) else []
                        originals = {str(x.get('jobNo')): x for x in raw_data if isinstance(x, dict)}
                        for row in payload['jobs']:
                            if not isinstance(row, dict):
                                warning('部分來源職缺格式不完整，已略過。')
                                reliable = False
                                continue
                            row = dict(row)
                            date = row.get('appear_date')
                            if isinstance(date, str) and re.fullmatch(r'\d{8}', date):
                                try: row['published_at'] = datetime.strptime(date, '%Y%m%d').date().isoformat()
                                except ValueError: row['published_at'] = None
                            elif date:
                                row['published_at'] = None
                                row.setdefault('filter_warnings', []).append('來源日期不明，未推測刊登時間。')
                            row.pop('appear_date', None)
                            original = originals.get(str(row.get('job_no')), {})
                            row['company_source_id'] = str(original['custNo']) if original.get('custNo') else None
                            if type(original.get('isAd')) is bool: row['is_ad'] = original['isAd']
                            if not safe_url(row.get('url'), 'www.104.com.tw') or not re.fullmatch('/job/[A-Za-z0-9]+', urlsplit(str(row.get('url'))).path):
                                warning('部分來源職缺網址不符，已略過。')
                                reliable = False
                                continue
                            page_rows.append(row)
                        page_state['next_page'] = page + 1
                        page_state['next_url'] = None
                        page_state['has_next'] = payload.get('has_next') is True
                    elif source == '1111':
                        try:
                            payload = await self.browser1111.call(dict(action='search', keyword=keyword, area_names=names, page=page))
                            page_rows = search_rows(payload, page)
                        except BrowserError as exc:
                            raise SourceError(exc.status, exc.message) from None
                        diagnostic['total_declared'] = payload['total']
                        page_state['total_declared'] = payload['total']
                        for limitation in payload.get('warnings', []):
                            warning(limitation)
                        page_state['next_page'] = page + 1
                        page_state['next_url'] = None
                        page_state['has_next'] = payload['has_next']
                        if page >= 5 and page_state['has_next']:
                            page_state['has_next'] = False
                            diagnostic['truncated'] = True
                            reliable = False
                            warning('1111 實驗來源最多查五頁，仍可能有更多職缺，請調整條件或至來源網站查看。')
                    else:
                        response = await self.request('GET', page_state['next_url'], headers={'Referer': 'https://www.518.com.tw/'})
                        parsed = parse_search_response(response.status_code, response.text)
                        if parsed['status'] not in ('success', 'partial', 'empty'):
                            raise SourceError(parsed['status'], '；'.join(parsed['limitations']) or None)
                        page_rows = parsed['items']
                        if parsed['status'] == 'partial':
                            reliable = False
                        for limitation in parsed['limitations']:
                            warning(limitation)
                        next_link = parsed['next_url']
                        page_state['next_page'] = page + 1
                        page_state['has_next'] = bool(next_link)
                        if next_link:
                            current_url = page_state['next_url']
                            next_link = urljoin(current_url, next_link)
                            if not _valid_518_cursor(next_link, page + 1, keyword, areas, current_url):
                                page_state['has_next'] = False
                                diagnostic['truncated'] = True
                                reliable = False
                                warning('下一頁改變篩選或網址，已保留目前結果並停止。')
                                page_state['next_url'] = None
                            else:
                                page_state['next_url'] = next_link
                        else:
                            page_state['next_url'] = None

                    page_state['visited'].append(page_cursor)
                    page_state['pages'] += 1
                    pages_fetched += 1
                    diagnostic['pages'] = page_state['pages']
                    diagnostic['pages_fetched'] = pages_fetched
                    jobs = []
                    for candidate in page_rows:
                        try:
                            normalized, warnings = normalize_jobs(source, [candidate])
                            jobs.extend(normalized)
                            result['filter_warnings'].extend(x for x in warnings if x not in result['filter_warnings'])
                        except ValueError:
                            reliable = False
                            warning('部分來源職缺格式不完整，已略過。')
                    if page_rows and not jobs and any(isinstance(x, dict) and x.get('is_ad') is not True for x in page_rows):
                        raise SourceError()
                    page_signature = '\x1f'.join(job.source_job_id for job in jobs)
                    if page_signature in page_state['page_signatures']:
                        warning('來源回傳重複的頁面內容，已停止續查以避免無限重複。')
                        reliable = False
                        page_state['has_next'] = False
                    else:
                        page_state['page_signatures'].append(page_signature)
                    add_jobs(jobs)
                    if len(rows) >= arguments.limit and (remaining or page_state['has_next']):
                        diagnostic['quota_limited'] = True
                        break
                if page_state['has_next'] and pages_fetched >= _MAX_NEW_PAGES:
                    diagnostic['quota_limited'] = True
                if page_state['has_next'] and len(rows) >= arguments.limit:
                    diagnostic['quota_limited'] = True
                diagnostic['truncated'] = diagnostic['truncated'] or diagnostic['quota_limited']
                diagnostic['baseline_eligible'] = reliable
                diagnostic['status'] = 'partial' if parser_warnings or diagnostic['truncated'] else ('success' if rows else 'empty')
            except SourceError as exc:
                if exc.status == 'unsupported':
                    page_state['has_next'] = False
                diagnostic.update(status='partial' if rows else exc.status, message=exc.message)
            except (ValueError, TypeError, KeyError, ImportError):
                diagnostic.update(status='partial' if rows else 'failed', message='來源格式或依賴異常，請管理者檢查。')

            page_state['remaining'] = [_job_state(job) for job in remaining]
            page_state['seen_ids'] = sorted(seen_ids)
            page_state['parser_warnings'] = list(parser_warnings)
            if parser_warnings:
                reliable = False
            diagnostic['parser_warnings'] = list(parser_warnings)
            diagnostic['parser_warning_count'] = len(parser_warnings)
            diagnostic['pages'] = page_state.get('pages', 0)
            diagnostic['pages_fetched'] = pages_fetched
            diagnostic['returned_count'] = len(rows)
            diagnostic['new_count'] = source_new
            diagnostic['duplicate_count'] = source_duplicates
            diagnostic['excluded_count'] = source_excluded
            diagnostic['message'] = diagnostic['message'] or ('；'.join(parser_warnings) if parser_warnings else None)
            diagnostic['baseline_eligible'] = bool(diagnostic['baseline_eligible'] and reliable)
            if diagnostic['status'] == 'failed' and rows:
                diagnostic['status'] = 'partial'
            result['items'].extend(rows)
            result['new_count'] += source_new
            result['duplicate_count'] += source_duplicates
            result['excluded_count'] += source_excluded
            result['source_results'].append(diagnostic)
            cursor_sources[source] = page_state

        result['can_continue'] = any(bool(state.get('remaining') or state.get('has_next')) for state in cursor_sources.values())
        if result['can_continue']:
            result['continuation'] = _cursor_result(arguments, cursor_sources)
        return result

    async def detail(self, job):
        url = str(job.url)
        from .job_normalize import _clean_text

        def clean(value, **kwargs):
            try:
                return _clean_text(value, **kwargs)
            except (TypeError, ValueError):
                return None

        if job.source == '104':
            if not safe_url(url, 'www.104.com.tw') or not re.fullmatch('/job/[A-Za-z0-9]+', urlsplit(url).path):
                raise SourceError('blocked')
            detail_id = urlsplit(url).path.rsplit('/', 1)[-1]
            payload, raw = await self.call104('get_job_detail', {'detail_id': detail_id})
            if not isinstance(payload, dict) or payload.get('error'):
                raise SourceError('failed')
            returned_id = clean(payload.get('job_no'), redact_contacts=False)
            title = (clean(payload.get('job_name')) or '')[:500]
            company = (clean(payload.get('company')) or '')[:500]
            text = clean(payload.get('description'))
            raw_data = raw.get('data') if isinstance(raw, dict) else None
            raw_header = raw_data.get('header') if isinstance(raw_data, dict) else None
            raw_ids = []
            for container in (raw_data, raw_header):
                if not isinstance(container, dict):
                    continue
                for key in ('jobNo', 'job_no'):
                    value = clean(container.get(key), redact_contacts=False)
                    if value and value not in raw_ids:
                        raw_ids.append(value)
            observed_ids = [value for value in (returned_id, *raw_ids) if value]
            id_matches = any(value == job.source_job_id for value in observed_ids)
            # URL-only reads have no saved job number; identity still requires the returned analysisUrl.
            id_conflict = (any(value != job.source_job_id for value in observed_ids)
                           if job.source_job_id is not None else len(set(observed_ids)) > 1)

            # The installed package maps data.header.analysisUrl only in its
            # raw response, while the public JobDetail mapping drops it. It is
            # useful evidence only when it is a safe 104 detail URL for the
            # saved short detail id. A payload ``url`` is intentionally ignored:
            # the package or a test transport may simply echo the request URL.
            analysis_url = None
            if isinstance(raw_header, dict):
                candidate = clean(raw_header.get('analysisUrl'), redact_contacts=False)
                if candidate:
                    candidate = urljoin('https://www.104.com.tw', candidate)
                    parsed_candidate = urlsplit(candidate)
                    path_parts = parsed_candidate.path.split('/')
                    if (safe_url(candidate, 'www.104.com.tw') and not parsed_candidate.query and
                            len(path_parts) == 5 and path_parts[1] == 'jobs' and
                            re.fullmatch(r'[A-Za-z0-9_-]+', path_parts[2] or '') and
                            path_parts[3] == 'analysis' and path_parts[4] == detail_id):
                        analysis_url = candidate
            analysis_url_matches = bool(analysis_url)
            # A request detail_id is only routing input. Identity requires a
            # returned source job number, or a source-returned analysis URL;
            # matching title/company fields alone is not unique evidence.
            verified = not id_conflict and (id_matches or analysis_url_matches)
            if id_conflict:
                limits = ['來源回傳的職缺 ID 與已保存職缺不符，詳細內容未覆蓋已保存職缺。']
            elif not verified:
                limits = ['來源未回傳可核對的職缺 ID 或可信的 analysisUrl，詳細內容未覆蓋已保存職缺。']
            else:
                limits = []
            if text and len(text) > 20000:
                text = text[:20000]; limits.append('詳細內容較長，僅顯示前20000字。')
            detail = {
                'source_job_id': job.source_job_id,
                'summary': text,
                'title': title,
                'company_name': company,
                'location': (clean(payload.get('location')) or '')[:500] or None,
                'salary_text': (clean(payload.get('salary')) or '')[:500] or None,
                'identity_evidence': {
                    'job_no': next((value for value in observed_ids if value == job.source_job_id), None),
                    'url': analysis_url if analysis_url_matches else None,
                },
            }
            has_content = any(detail.get(key) for key in ('summary', 'title', 'company_name', 'location', 'salary_text'))
            return dict(job_id=job.source_job_id, status='success' if has_content else 'failed',
                        identity_verified=verified, detail=detail, limitations=limits)
        if job.source == '1111':
            if not job_url(url, job.source_job_id) or not job.company_source_id:
                raise SourceError('unsupported', '1111 職缺缺少可靠的公司識別資料，未請求詳細頁。')
            try:
                payload = await self.browser1111.call(dict(action='detail', job=dict(
                    id=job.source_job_id, title=job.title, company=job.company_name, url=url,
                    companyUrl=f'https://www.1111.com.tw/corp/{job.company_source_id}/')))
            except BrowserError as exc:
                raise SourceError(exc.status, exc.message) from None
            detail = payload.get('detail')
            if not isinstance(detail, dict):
                raise SourceError('unsupported', '1111 詳細頁格式無法辨識。')
            title, company = clean(detail.get('title')), clean(detail.get('company'))
            text = clean(detail.get('description'))
            links = detail.get('companyLinks', detail.get('companyUrls', []))
            verified = (detail.get('id') == job.source_job_id and job_url(detail.get('url'), job.source_job_id)
                        and title == job.title and company == job.company_name and isinstance(links, list)
                        and any(company_id(link) == job.company_source_id for link in links))
            limitations = [] if verified else ['1111 詳細頁職缺或公司識別與已保存職缺不符，未覆蓋已保存職缺。']
            if text and len(text) > 20000:
                text = text[:20000]
                limitations.append('詳細內容較長，僅顯示前20000字。')
            return dict(job_id=job.source_job_id, status='success' if text else 'failed', identity_verified=bool(verified and text),
                        detail=dict(source_job_id=job.source_job_id, summary=text, title=title, company_name=company,
                                    location=None, salary_text=None,
                                    identity_evidence=dict(job_no=detail.get('id'), url=detail.get('url'))), limitations=limitations)
        if not safe_url(url, 'www.518.com.tw', f'/job-{job.source_job_id}.html'):
            raise SourceError('blocked')
        response = await self.request('GET', url)
        result = parse_detail_response(response.status_code, response.text, job.source_job_id, expected_url=url)
        detail = result.get('detail') if isinstance(result, dict) else None
        verified = bool(result.get('identity_verified')) if isinstance(result, dict) else False
        if verified and isinstance(detail, dict):
            if detail.get('title') != job.title or detail.get('company_name') != job.company_name:
                verified = False
                result.setdefault('limitations', []).append('詳細頁職稱或公司與已保存職缺不符，未覆蓋已保存職缺。')
        if isinstance(result, dict):
            result['job_id'] = job.source_job_id
            result['identity_verified'] = verified
            if not verified and not result.get('limitations'):
                result['limitations'] = ['詳細頁未提供可核對的職缺識別資訊，未覆蓋已保存職缺。']
        return result
