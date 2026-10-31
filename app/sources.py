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
from .source1111 import Browser1111, BrowserError, search_batch, SINGLE_AREA_SECONDS, STARTUP_SECONDS, WORKER_GRACE_SECONDS, MAX_AREAS, MAX_SEARCH_PAGES, job_url, company_id
from .store import Criteria, Input, Job


def enabled_sources(services):
    """104 is always on; 518 defaults on and experimental 1111 defaults off."""
    return (['104'] + (['518'] if services.get('source_518_enabled') is not False else [])
            + (['1111'] if services.get('source_1111_enabled') is True else []))


# A source number standing alone, not an address, wage or other quantity ("104號"、"時薪518"、"A1111B").
_SOURCE_MENTION = re.compile(
    r'(?<![0-9a-z$])(?:(?:www\.)?(?:104|518|1111)\.com\.tw(?![0-9a-z-]|\.[0-9a-z-])|'
    r'(?<!\.)(?<!薪)(?:104|518|1111)(?![0-9a-z]|\.[0-9a-z]|\s*(?:號|号|巷|弄|樓|楼|路|街|段|區|元|塊|萬|千|百|個|天|年|月|小時|分)))')


def mentions_source(text):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    # Links with paths are references, and digit runs with punctuation are quantities/phone numbers.
    text = re.sub(r'(?:https?://)?(?:www\.)?(?:104|518|1111)\.com\.tw/[a-z0-9._~:/?#@!$&()*+,;=%-]*', ' ', text)
    text = re.sub(r'(?<![0-9])[0-9]+(?:[,\-][0-9]+)+(?![0-9])', ' ', text)
    return bool(_SOURCE_MENTION.search(text))


def requested_sources(arguments, enabled, user_text, continuation=False):
    """Sources a search asks for, before disabled ones are dropped.

    A new search queries every enabled source unless any user message through the current turn names a source ("只查 104"、"不要 1111"):
    models narrowed the list on their own, which silently skipped sources the admin had turned on. A continuation
    keeps its saved sources, since its cursor has no position for a newly added one.
    """
    requested = list(arguments.sources)
    texts = [user_text] if isinstance(user_text, str) else (user_text or [])
    mentioned = any(mentions_source(text) for text in texts)
    if not continuation and ('sources' not in arguments.model_fields_set or not mentioned):
        requested += [source for source in enabled if source not in requested]
    return requested


class SearchInput(Input):
    criteria: Criteria
    sources: list[str] = Field(default_factory=lambda: ['104', '518'], min_length=1, max_length=3,
        description='通常省略：一律查詢後台已啟用的全部來源（104，以及已啟用的 518、1111）。只有這段對話截至當回合的使用者訊息曾明確指定或排除某個人力銀行時才填，且只有那時才會限縮。1111 為實驗來源，可能因人機驗證受阻。')
    limit: StrictInt = Field(default=20, ge=1, le=20,
        description='每來源回傳筆數，最多填20；多地區時程式會自動提高每來源的有效回傳列數。')
    search_id: str | None = Field(default=None, min_length=1, max_length=100)
    exclude_companies: list[str] = Field(default_factory=list, max_length=20,
        description='使用者要求排除的公司。同時列出使用者說法與搜尋結果中出現的公司全名（例如簡稱與全名各一項）；比對忽略全半形、空白及「股份有限公司」等字樣，任一項包含於公司名稱即排除。排除會套用到這段對話之後的所有搜尋與續查。')

    include_companies: list[str] = Field(default_factory=list, max_length=5,
        description='使用者指名要找某公司的職缺時必須填 include_companies，不能只把公司名放進 keyword；列出使用者說法與公司全名／簡稱各一項，只保留公司名稱符合且屬於該公司本身的職缺，不含廠區駐廠、現場、門市或承包商的職缺。來源只能用關鍵字查，criteria.keyword仍須填該公司的主要名稱。沒有符合的就如實說明「這次查到的職缺裡沒有合適的」，不放寬。')

    def model_post_init(self, context):
        if any(x not in ('104', '518', '1111') for x in self.sources) or len(set(self.sources)) != len(self.sources):
            raise ValueError('來源不符或重複')
        if any(not name or len(name) > 80 or len(company_key(name)) < 2 for name in self.exclude_companies):
            raise ValueError('排除公司名稱須為2～80字')
        self.exclude_companies = list(dict.fromkeys(self.exclude_companies))
        if any(not name or len(name) > 80 or len(company_key(name)) < 2 for name in self.include_companies):
            raise ValueError('指名公司名稱須為2～80字')
        self.include_companies = list(dict.fromkeys(self.include_companies))


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
                            response = requests.get(url, params=params, timeout=_HTTP_TIMEOUT_SECONDS, impersonate='chrome',
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


_CONTINUATION_VERSION = 2
_MAX_NEW_PAGES = 5
_COMPANY_1111_MAX_PAGES = 2
AREA_ROW_FLOOR = 5
MULTI_AREA_ROW_CAP = 50
_HTTP_TIMEOUT_SECONDS = 30
_AREA_STATUSES = {'success', 'empty', 'blocked', 'failed', 'unsupported', 'not_queried'}


def _criteria_state(arguments):
    state = {
        'keyword': arguments.criteria.keyword,
        'area_codes': list(arguments.criteria.area_codes),
        'min_monthly_salary': arguments.criteria.min_monthly_salary,
    }
    if arguments.criteria.remote_only:
        state['remote_only'] = True
    return state


def _page_size(arguments):
    # DailySearchInput keeps its existing 100-result cap over five 20-row pages.
    return min(20, arguments.limit)


def _job_state(job):
    return job.model_dump(mode='json')


def _cursor_error(message):
    raise ValueError(message)


def _legacy_source_state(source, item, area_key, area_name):
    if source == '1111':
        active = item.get('active_areas') or []
        pending = item.get('pending_next_areas') or []
        if any(name != area_name for name in [*active, *pending]):
            _cursor_error('續查位置版本不支援，請從原搜尋重新開始。')
        totals = item.get('area_totals', {})
        total = totals.get(area_name, item.get('total_declared')) if isinstance(totals, dict) else None
    else:
        total = item.get('total_declared')
    seen_ids = item.get('seen_ids', [])
    remaining = item.get('remaining', [])
    pages = int(item.get('pages', 0))
    has_next = bool(item.get('has_next', True))
    queried = pages > 0
    warnings = item.get('parser_warnings', [])
    status = ('empty' if queried and total == 0 else
              'success' if queried and (seen_ids or remaining) else 'failed' if queried else 'not_queried')
    area = {
        'next_page': int(item.get('next_page', 1)),
        'next_url': item.get('next_url'),
        'has_next': has_next,
        'visited': item.get('visited', []),
        'page_signatures': item.get('page_signatures', []),
        'remaining': remaining,
        'pages': pages,
        'total_declared': total,
        'returned_count': 0,
        'queried': queried,
        'has_success': queried and status != 'failed',
        'status': status,
        'message': None,
        'warnings': warnings,
    }
    return {
        'page_size': item.get('page_size'),
        'seen_ids': seen_ids,
        'parser_warnings': warnings,
        'areas': {area_key: area},
    }


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
    if not isinstance(state, dict) or state.get('version') not in (1, _CONTINUATION_VERSION):
        _cursor_error('續查位置版本不支援，請從原搜尋重新開始。')
    old_version = state['version'] == 1
    if state.get('criteria') != _criteria_state(arguments):
        _cursor_error('續查條件已變更，請從新搜尋開始。')
    saved_sources = state.get('sources')
    if not isinstance(saved_sources, dict) or not set(arguments.sources) <= set(saved_sources):
        _cursor_error('續查來源已變更，請從原搜尋重新開始。')
    area_codes = list(arguments.criteria.area_codes)
    area_keys = area_codes or ['all']
    area_names = {item['code']: item['name'] for item in area_catalog()}
    if not area_codes:
        area_names['all'] = ''
    request_page_size = _page_size(arguments)
    if old_version and len(area_keys) != 1:
        _cursor_error('續查位置版本不支援，請從原搜尋重新開始。')
    for source in arguments.sources:
        item = saved_sources.get(source)
        if not isinstance(item, dict) or item.get('page_size') != request_page_size:
            _cursor_error('續查每頁筆數已變更，請沿用原搜尋筆數。')
        if not isinstance(item.get('seen_ids', []), list) or not all(isinstance(x, str) for x in item.get('seen_ids', [])):
            _cursor_error('續查已取得的職缺識別資料無效。')
        if not isinstance(item.get('parser_warnings', []), list) or not all(isinstance(x, str) for x in item.get('parser_warnings', [])):
            _cursor_error('續查來源診斷資料無效，請從原搜尋重新開始。')
        if 'areas' not in item:
            if not old_version:
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
            key = area_keys[0]
            item = _legacy_source_state(source, item, key, area_names.get(key, ''))
            saved_sources[source] = item
        areas = item.get('areas')
        if not isinstance(areas, dict) or set(areas) != set(area_keys):
            _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
        for area in areas.values():
            if (not isinstance(area, dict) or not isinstance(area.get('status'), str) or
                    area['status'] not in _AREA_STATUSES):
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
            for name in ('visited', 'page_signatures'):
                if not isinstance(area.get(name, []), list) or not all(isinstance(x, str) for x in area.get(name, [])):
                    _cursor_error('續查頁面紀錄無效，請從原搜尋重新開始。')
            if not isinstance(area.get('remaining', []), list):
                _cursor_error('續查尚未顯示的職缺資料無效。')
            if not isinstance(area.get('warnings', []), list) or not all(isinstance(x, str) for x in area.get('warnings', [])):
                _cursor_error('續查來源診斷資料無效，請從原搜尋重新開始。')
            if type(area.get('has_next')) is not bool or type(area.get('queried')) is not bool or type(area.get('has_success')) is not bool:
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
            if area.get('message') is not None and not isinstance(area.get('message'), str):
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
            total = area.get('total_declared')
            if 'total_declared' not in area or total is not None and (type(total) is not int or total < 0):
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
            for name, minimum in (('next_page', 1), ('pages', 0), ('returned_count', 0)):
                try:
                    area[name] = int(area.get(name, minimum))
                except (TypeError, ValueError, OverflowError):
                    _cursor_error('續查頁碼無效，請從原搜尋重新開始。')
                if area[name] < minimum:
                    _cursor_error('續查頁碼無效，請從原搜尋重新開始。')
            if area.get('next_url') is not None and not isinstance(area.get('next_url'), str):
                _cursor_error('續查區域狀態無效，請從原搜尋重新開始。')
    state['version'] = _CONTINUATION_VERSION
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

    async def close(self):
        await self.browser1111.close()

    async def request(self, method, url, *, host=None, max_retries=1, **kwargs):
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError('max_retries must be a non-negative integer')
        host = host or urlsplit(url).hostname
        if not safe_url(url, host):
            raise SourceError('blocked', '來源網址不符，已停止請求。')
        path = urlsplit(url).path
        lock = self.locks.setdefault(host, asyncio.Lock())
        if self.transport is not None:
            client_context = httpx.AsyncClient(transport=self.transport, timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False)
            network_errors = (httpx.RequestError, TimeoutError)
        else:
            from curl_cffi.requests import AsyncSession
            from curl_cffi.requests.exceptions import RequestException
            # Reuse job104's installed TLS-verifying transport; no certificate bypass.
            client_context = AsyncSession(timeout=_HTTP_TIMEOUT_SECONDS, allow_redirects=False, trust_env=False, verify=True, impersonate='chrome')
            network_errors = (RequestException, TimeoutError)
        async with lock, client_context as client:
            retries, hops = 0, 0
            while True:
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last.get(host, 0))))
                self.last[host] = time.monotonic()
                try:
                    async with asyncio.timeout(_HTTP_TIMEOUT_SECONDS):
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

    async def search(self, arguments, *, continuation=None, exclude_ids=None, exclude_companies=None, deadline=None, finish_reserve=0):
        result = {
            'items': [], 'source_results': [], 'area_results': [], 'filter_warnings': [],
            'new_count': 0, 'duplicate_count': 0, 'excluded_count': 0,
            'can_continue': False, 'continuation': None,
        }
        catalog = {item['code']: item['name'] for item in area_catalog()}
        area_specs = ([dict(key=code, area_code=code, area_name=catalog.get(code, f'未辨識地區（{code}）'),
                            query_name=catalog.get(code)) for code in arguments.criteria.area_codes]
                      if arguments.criteria.area_codes else
                      [dict(key='all', area_code=None, area_name='全台', query_name='')])
        limit = max(arguments.limit, min(AREA_ROW_FLOOR * len(area_specs), MULTI_AREA_ROW_CAP,
                                        _page_size(arguments) * len(area_specs))) if len(area_specs) > 1 else arguments.limit
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
            result['area_results'] = [dict(area_code=area['area_code'], area_name=area['area_name'], source=source,
                status='not_queried', returned_count=0, total_declared=None, message=str(exc), completed=False)
                for area in area_specs for source in arguments.sources]
            return result
        excluded = ({str(exclude_ids)} if isinstance(exclude_ids, str) else
                    {str(value) for value in (exclude_ids or []) if value is not None})
        company_keys = [key for key in map(company_key, exclude_companies or []) if len(key) >= 2]
        include_keys = [key for key in map(company_key, arguments.include_companies) if len(key) >= 2]
        company_candidates = company_matches = 0
        saved_sources = cursor.get('sources', {}) if cursor else {}
        cursor_sources = {}
        area_rows = {}

        def is_excluded(job):
            return (bool(_source_job_keys(job) & excluded) or company_excluded(job.company_name, company_keys)
                    or bool(include_keys) and not company_excluded(job.company_name, include_keys))

        def new_area_state():
            return {
                'next_page': 1, 'next_url': None, 'has_next': True, 'visited': [], 'page_signatures': [],
                'remaining': [], 'pages': 0, 'total_declared': None, 'returned_count': 0,
                'queried': False, 'has_success': False, 'status': 'not_queried', 'message': None, 'warnings': [],
            }

        for source in arguments.sources:
            diagnostic = dict(
                source=source, status='failed', truncated=False, quota_limited=False,
                pages=0, pages_fetched=0, returned_count=0, new_count=0,
                duplicate_count=0, excluded_count=0, total_declared=None,
                parser_warning_count=0, parser_warnings=[], message=None,
                baseline_eligible=False,
            )
            source_state = dict(saved_sources[source]) if cursor else {
                'page_size': _page_size(arguments), 'seen_ids': [], 'parser_warnings': [],
                'areas': {area['key']: new_area_state() for area in area_specs},
            }
            area_states = source_state['areas']
            queues = {}
            rows = []
            seen_ids = set(source_state.get('seen_ids', []))
            parser_warnings = list(source_state.get('parser_warnings', []))
            source_new = source_duplicates = source_excluded = source_remote_excluded = 0
            returned_this_call = {area['key']: 0 for area in area_specs}
            pages_fetched = 0
            failed_this_call = set()
            source_blocked = False
            budget_limited = False
            page_limited = False

            def warning(message, area_state=None):
                if message:
                    if message not in parser_warnings:
                        parser_warnings.append(message)
                    if area_state is not None and message not in area_state['warnings']:
                        area_state['warnings'].append(message)

            def set_unsupported(area, message):
                state = area_states[area['key']]
                state.update(status='unsupported', message=message, has_next=False, remaining=[])
                queues[area['key']] = []

            try:
                for area in area_specs:
                    state = area_states[area['key']]
                    pending = []
                    for raw_job in state.get('remaining', []):
                        try:
                            pending.append(Job.model_validate(raw_job))
                        except (ValueError, TypeError):
                            raise SourceError('unsupported', '續查尚未顯示的職缺資料無效，請從原搜尋重新開始。') from None
                    queues[area['key']] = pending
                    state['remaining'] = []
                    if area['query_name'] is None:
                        set_unsupported(area, '地區代碼無法辨識，請重新選擇地區。')

                if arguments.criteria.remote_only and source != '104':
                    for area in area_specs:
                        set_unsupported(area, f'{source} 尚未支援遠端工作篩選，本次未查。')
                elif source == '1111' and len(area_specs) > MAX_AREAS:
                    for area in area_specs:
                        set_unsupported(area, '1111 一次最多查 10 個地區，請縮小範圍。')
                elif source == '518':
                    mapped_areas = [area for area in area_specs if area['query_name'] and
                                    (area_states[area['key']]['has_next'] or not area_states[area['key']]['queried'])]
                    if mapped_areas and self.regions518 is None:
                        if deadline is not None and deadline - time.monotonic() - finish_reserve < _HTTP_TIMEOUT_SECONDS:
                            budget_limited = True
                            for area in mapped_areas:
                                state = area_states[area['key']]
                                state['message'] = '時間不足，本次未查此地區；可按續查補查。'
                        else:
                            try:
                                response = await self.request('GET', 'https://statics.518.com.tw/dist/js/common/menu/location.js')
                                self.regions518 = region_map(response.text)
                            except SourceError as exc:
                                source_blocked = exc.status == 'blocked'
                                for area in mapped_areas:
                                    state = area_states[area['key']]
                                    if exc.status == 'unsupported':
                                        set_unsupported(area, exc.message)
                                    else:
                                        state.update(status=exc.status, message=exc.message, queried=True, has_next=True)
                                        failed_this_call.add(area['key'])
                    if self.regions518 is not None:
                        for area in mapped_areas:
                            if area['query_name'] not in self.regions518:
                                set_unsupported(area, '518 尚無此地區的可靠對應，未改查全國。')

                def add_jobs(area, jobs):
                    nonlocal source_duplicates, source_excluded
                    queue = queues[area['key']]
                    for job in jobs:
                        if job.source_job_id in seen_ids:
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
                        seen_ids.add(job.source_job_id)
                        queue.append(job)

                def consume_queues():
                    nonlocal source_new, source_excluded
                    progressed = True
                    while len(rows) < limit and progressed:
                        progressed = False
                        for area in area_specs:
                            queue = queues[area['key']]
                            if queue and len(rows) < limit:
                                job = queue.pop(0)
                                if is_excluded(job):
                                    source_excluded += 1
                                else:
                                    rows.append(job)
                                    area_states[area['key']]['returned_count'] += 1
                                    returned_this_call[area['key']] += 1
                                    source_new += 1
                                progressed = True

                def normalize_candidates(area, candidates):
                    nonlocal company_candidates, company_matches
                    jobs = []
                    for candidate in candidates:
                        try:
                            normalized, warnings = normalize_jobs(source, [candidate])
                            jobs.extend(normalized)
                            result['filter_warnings'].extend(x for x in warnings if x not in result['filter_warnings'])
                        except ValueError:
                            warning('部分來源職缺格式不完整，已略過。', area_states[area['key']])
                    if candidates and not jobs and any(isinstance(item, dict) and item.get('is_ad') is not True for item in candidates):
                        raise SourceError()
                    if include_keys:
                        company_candidates += len(jobs)
                        company_matches += sum(company_excluded(job.company_name, include_keys) for job in jobs)
                    return jobs

                def apply_page(area, state, page, page_cursor, page_result):
                    nonlocal pages_fetched
                    pages_fetched += 1
                    state['queried'] = True
                    state['has_success'] = True
                    state['pages'] += 1
                    state['total_declared'] = page_result['total_declared']
                    state['next_page'] = page + 1
                    state['next_url'] = page_result.get('next_url')
                    state['has_next'] = page_result['has_next']
                    state['visited'].append(page_cursor)
                    for message in page_result['warnings']:
                        warning(message, state)
                    jobs = normalize_candidates(area, page_result['rows'])
                    signature_ids = page_result['signature_ids'] or [job.source_job_id for job in jobs]
                    signature = '\x1f'.join(signature_ids)
                    if signature in state['page_signatures']:
                        warning('來源回傳重複的頁面內容，已停止續查以避免重複請求。', state)
                        state['has_next'] = False
                    else:
                        state['page_signatures'].append(signature)
                    state['status'] = 'empty' if state['total_declared'] == 0 and not jobs else 'success'
                    state['message'] = page_result.get('message')
                    add_jobs(area, jobs)

                async def fetch_104(area, page):
                    nonlocal source_remote_excluded, source_excluded
                    payload, raw = await self.call104('search_jobs', dict(
                        keyword=arguments.criteria.keyword,
                        area=[area['query_name']] if area['query_name'] else [],
                        page=page, page_size=_page_size(arguments),
                        salary_min=arguments.criteria.min_monthly_salary,
                        **({'remote': True} if arguments.criteria.remote_only else {}),
                    ))
                    if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list) or type(payload.get('total')) is not int or payload['total'] < 0:
                        raise SourceError()
                    if not payload['jobs'] and payload['total'] > 0:
                        raise SourceError()
                    raw_data = (raw or {}).get('data', []) if isinstance(raw, dict) else []
                    originals = {str(item.get('jobNo')): item for item in raw_data if isinstance(item, dict)}
                    candidates, signature_ids, warnings = [], [], []
                    for item in payload['jobs']:
                        if not isinstance(item, dict):
                            warnings.append('部分來源職缺格式不完整，已略過。')
                            continue
                        item = dict(item)
                        signature_ids.append(str(item.get('job_no')))
                        if arguments.criteria.remote_only and (type(item.get('remote')) is not int or item['remote'] not in (1, 2)):
                            source_remote_excluded += 1
                            source_excluded += 1
                            continue
                        date = item.get('appear_date')
                        if isinstance(date, str) and re.fullmatch(r'\d{8}', date):
                            try:
                                item['published_at'] = datetime.strptime(date, '%Y%m%d').date().isoformat()
                            except ValueError:
                                item['published_at'] = None
                        elif date:
                            item['published_at'] = None
                            item.setdefault('filter_warnings', []).append('來源日期不明，未推測刊登時間。')
                        item.pop('appear_date', None)
                        original = originals.get(str(item.get('job_no')), {})
                        item['company_source_id'] = str(original['custNo']) if original.get('custNo') else None
                        if type(original.get('isAd')) is bool:
                            item['is_ad'] = original['isAd']
                        if not safe_url(item.get('url'), 'www.104.com.tw') or not re.fullmatch('/job/[A-Za-z0-9]+', urlsplit(str(item.get('url'))).path):
                            warnings.append('部分來源職缺網址不符，已略過。')
                            continue
                        candidates.append(item)
                    return dict(rows=candidates, total_declared=payload['total'],
                        has_next=payload.get('has_next') is True, next_url=None,
                        signature_ids=signature_ids, warnings=warnings)

                async def fetch_518(area, state, page, areas_value):
                    keyword = arguments.criteria.keyword
                    if not state['next_url'] and state['next_page'] == 1:
                        state['next_url'] = 'https://www.518.com.tw/job-index-P-1.html?' + urlencode({'ad': keyword, 'aa': areas_value})
                    if not _valid_518_cursor(state['next_url'], page, keyword, areas_value):
                        raise SourceError('blocked', '續查網址不符合原始搜尋條件，已停止請求。')
                    response = await self.request('GET', state['next_url'], headers={'Referer': 'https://www.518.com.tw/'})
                    parsed = parse_search_response(response.status_code, response.text)
                    if parsed['status'] not in ('success', 'partial', 'empty'):
                        raise SourceError(parsed['status'], '；'.join(parsed['limitations']) or None)
                    warnings = list(parsed['limitations'])
                    next_url = parsed['next_url']
                    has_next = bool(next_url)
                    message = None
                    if next_url:
                        next_url = urljoin(state['next_url'], next_url)
                        if not _valid_518_cursor(next_url, page + 1, keyword, areas_value, state['next_url']):
                            has_next = False
                            next_url = None
                            message = '下一頁改變篩選或網址，已保留目前結果並停止。'
                            warnings.append(message)
                            diagnostic['truncated'] = True
                    return dict(rows=parsed['items'], total_declared=0 if parsed['status'] == 'empty' else None,
                        has_next=has_next, next_url=next_url,
                        signature_ids=[item['source_job_id'] for item in parsed['items']], warnings=warnings,
                        message=message)

                def stop_for_http_budget():
                    for area in area_specs:
                        state = area_states[area['key']]
                        if state['status'] in ('unsupported', 'blocked', 'failed') or not state['has_next']:
                            continue
                        state['message'] = '時間不足，本次未查此地區；可按續查補查。'
                        if not state['queried']:
                            state['status'] = 'not_queried'

                consume_queues()
                if source in ('104', '518') and not budget_limited and not source_blocked:
                    max_pages = max(_MAX_NEW_PAGES, len(area_specs))
                    while not source_blocked and len(rows) < limit and pages_fetched < max_pages:
                        active = [area for area in area_specs
                                  if area_states[area['key']]['has_next'] and area['key'] not in failed_this_call]
                        if not active:
                            break
                        deadline_stop = False
                        for area in active:
                            if pages_fetched >= max_pages:
                                break
                            state = area_states[area['key']]
                            if deadline is not None and deadline - time.monotonic() - finish_reserve < _HTTP_TIMEOUT_SECONDS:
                                budget_limited = deadline_stop = True
                                stop_for_http_budget()
                                break
                            page = state['next_page']
                            areas_value = (self.regions518 or {}).get(area['query_name'], '') if source == '518' and area['query_name'] else ''
                            page_cursor = str(page) if source == '104' else state['next_url'] or 'page-1'
                            if page_cursor in state['visited']:
                                warning('來源下一頁位置重複，已停止續查以避免重複請求。', state)
                                state.update(message='來源下一頁位置重複，已停止續查以避免重複請求。', has_next=False)
                                failed_this_call.add(area['key'])
                                continue
                            try:
                                if source == '104':
                                    page_result = await fetch_104(area, page)
                                else:
                                    page_result = await fetch_518(area, state, page, areas_value)
                                apply_page(area, state, page, page_cursor, page_result)
                            except SourceError as exc:
                                state.update(status=exc.status, message=exc.message, queried=True,
                                    has_next=exc.status != 'unsupported')
                                failed_this_call.add(area['key'])
                                if exc.status == 'blocked':
                                    source_blocked = True
                                    break
                            except (ValueError, TypeError, KeyError, ImportError):
                                state.update(status='failed', message='來源格式或依賴異常，請管理者檢查。', queried=True, has_next=True)
                                failed_this_call.add(area['key'])
                        consume_queues()
                        if source_blocked or deadline_stop or len(rows) >= limit:
                            break
                    if pages_fetched >= max_pages and any(area_states[area['key']]['has_next'] for area in area_specs):
                        page_limited = True
                elif source == '1111':
                    page_cap = _COMPANY_1111_MAX_PAGES if include_keys else MAX_SEARCH_PAGES
                    max_pages = max(_COMPANY_1111_MAX_PAGES if include_keys else _MAX_NEW_PAGES, len(area_specs))
                    while not source_blocked and len(rows) < limit and pages_fetched < max_pages:
                        pending = [area for area in area_specs
                                   if area_states[area['key']]['has_next'] and area['key'] not in failed_this_call]
                        if not pending:
                            break
                        page = min(area_states[area['key']]['next_page'] for area in pending)
                        active = [area for area in pending if area_states[area['key']]['next_page'] == page]
                        budget = deadline - time.monotonic() - finish_reserve if deadline is not None else None
                        minimum = SINGLE_AREA_SECONDS + STARTUP_SECONDS + WORKER_GRACE_SECONDS
                        if budget is not None and budget < minimum:
                            budget_limited = True
                            for area in active:
                                state = area_states[area['key']]
                                state.update(message='時間不足，本次未查此地區；可按續查補查。')
                            break
                        names = [area['query_name'] for area in active if area['query_name']]
                        call = dict(action='search', keyword=arguments.criteria.keyword, area_names=names, page=page)
                        try:
                            payload = await self.browser1111.call(call, **({'timeout': budget} if budget is not None else {}))
                            batch = search_batch(payload, page, names, {
                                area['query_name'] or '': area_states[area['key']]['total_declared']
                                for area in active})
                        except BrowserError as exc:
                            pages_fetched += 1
                            for area in active:
                                state = area_states[area['key']]
                                if exc.status == 'time_limited':
                                    state.update(message=f"1111 {area['area_name']}：{exc.message}")
                                else:
                                    state.update(status=exc.status, message=f"1111 {area['area_name']}：{exc.message}",
                                        queried=True, has_next=exc.status != 'unsupported')
                                    failed_this_call.add(area['key'])
                            source_blocked = exc.status == 'blocked'
                            if exc.status == 'time_limited':
                                budget_limited = True
                                break
                            continue
                        pages_fetched += 1
                        deferred = False
                        for area, area_page in zip(active, batch['areas']):
                            state = area_states[area['key']]
                            for message in area_page['warnings']:
                                warning(f"1111 {area['area_name']}：{message}", state)
                            status = area_page['status']
                            if status == 'not_queried':
                                budget_limited = True
                                state.update(message=f"1111 {area['area_name']}：{area_page['message']}" if area_page['message'] else None)
                                deferred = True
                                continue
                            if status in ('blocked', 'failed', 'unsupported'):
                                state.update(status=status,
                                    message=f"1111 {area['area_name']}：{area_page['message']}" if area_page['message'] else None,
                                    queried=True, has_next=status != 'unsupported')
                                failed_this_call.add(area['key'])
                                source_blocked = source_blocked or status == 'blocked'
                                continue
                            try:
                                jobs = normalize_candidates(area, area_page['rows'])
                            except SourceError as exc:
                                state.update(status=exc.status, message=exc.message, queried=True,
                                    has_next=exc.status != 'unsupported')
                                failed_this_call.add(area['key'])
                                source_blocked = source_blocked or exc.status == 'blocked'
                                continue
                            state['queried'] = True
                            state['has_success'] = True
                            state['pages'] += 1
                            state['total_declared'] = area_page['total_declared']
                            state['next_page'] = page + 1
                            state['has_next'] = area_page['has_next']
                            state['message'] = area_page['message']
                            state['status'] = status
                            signature_ids = [str(item.get('source_job_id')) for item in area_page['rows']]
                            signature = '\x1f'.join(signature_ids)
                            if signature in state['page_signatures']:
                                warning('來源回傳重複的頁面內容，已停止續查以避免重複。', state)
                                state['has_next'] = False
                            else:
                                state['page_signatures'].append(signature)
                            add_jobs(area, jobs)
                            if page >= page_cap and state['has_next']:
                                diagnostic['truncated'] = True
                                state['has_next'] = False
                                if include_keys:
                                    message = f'1111指名公司的搜尋只查前{_COMPANY_1111_MAX_PAGES}頁，未再翻頁。'
                                    if message not in result['filter_warnings']:
                                        result['filter_warnings'].append(message)
                                else:
                                    warning('1111 實驗來源最多查五頁，仍可能有更多職缺，請調整條件或至來源網站查看。', state)
                        consume_queues()
                        if deferred or len(rows) >= limit:
                            break
                    if pages_fetched >= max_pages and any(area_states[area['key']]['has_next'] for area in area_specs):
                        page_limited = True

                if source_remote_excluded:
                    message = f'依遠端條件排除 {source_remote_excluded} 筆非遠端職缺。'
                    if message not in result['filter_warnings']:
                        result['filter_warnings'].append(message)

                for area in area_specs:
                    state = area_states[area['key']]
                    state['remaining'] = [_job_state(job) for job in queues[area['key']]]
                    complete = not state['has_next'] and not state['remaining']
                    if state['status'] == 'unsupported':
                        status = 'unsupported'
                    elif state['status'] in ('blocked', 'failed'):
                        status = 'partial' if returned_this_call[area['key']] or state['remaining'] else state['status']
                    elif state['status'] == 'not_queried' and not state['queried']:
                        status = 'not_queried'
                    elif not complete or state['warnings']:
                        status = 'partial'
                    elif state['queried'] and state['total_declared'] == 0 and state['returned_count'] == 0:
                        status = 'empty'
                    elif state['returned_count'] or state['has_success']:
                        status = 'success'
                    else:
                        status = 'failed' if state['queried'] else 'not_queried'
                    message = state['message'] or ('；'.join(state['warnings']) if state['warnings'] else None)
                    area_rows[(area['key'], source)] = dict(
                        area_code=area['area_code'], area_name=area['area_name'], source=source,
                        status=status, returned_count=state['returned_count'],
                        total_declared=state['total_declared'], message=message, completed=complete,
                    )

                statuses = [area_rows[(area['key'], source)]['status'] for area in area_specs]
                if all(status == 'unsupported' for status in statuses):
                    source_status = 'unsupported'
                elif len(set(statuses)) == 1 and statuses[0] in ('blocked', 'failed'):
                    source_status = statuses[0]
                elif all(status == 'not_queried' for status in statuses) and not (rows or seen_ids or result['items']):
                    source_status = 'failed'
                elif any(status in ('partial', 'blocked', 'failed', 'unsupported', 'not_queried') for status in statuses):
                    source_status = 'partial'
                else:
                    source_status = 'success' if rows else 'empty'
                total_values = [area_states[area['key']]['total_declared'] for area in area_specs
                                if area_states[area['key']]['total_declared'] is not None]
                remaining_any = any(area_states[area['key']]['remaining'] for area in area_specs)
                incomplete_any = any(not area_rows[(area['key'], source)]['completed'] for area in area_specs)
                quota_limited = page_limited or budget_limited or len(rows) >= limit and (remaining_any or incomplete_any)
                declared_total = sum(total_values) if source != '518' and len(total_values) == len(area_specs) else None
                diagnostic.update(
                    status=source_status,
                    truncated=bool(diagnostic['truncated'] or quota_limited),
                    quota_limited=bool(quota_limited),
                    pages=sum(area_states[area['key']]['pages'] for area in area_specs),
                    pages_fetched=pages_fetched,
                    returned_count=len(rows), new_count=source_new,
                    duplicate_count=source_duplicates, excluded_count=source_excluded,
                    total_declared=declared_total,
                    parser_warning_count=len(parser_warnings), parser_warnings=parser_warnings,
                    message='；'.join(dict.fromkeys(
                        [area_rows[(area['key'], source)]['message'] for area in area_specs
                         if area_rows[(area['key'], source)]['message']] + parser_warnings)) or None,
                    baseline_eligible=all(area_states[area['key']]['queried'] and
                        area_rows[(area['key'], source)]['status'] not in
                        ('blocked', 'failed', 'unsupported', 'not_queried') for area in area_specs)
                        and not parser_warnings and not budget_limited and not failed_this_call,
                )
                if source_remote_excluded:
                    diagnostic['remote_excluded_count'] = source_remote_excluded
                result['items'].extend(rows)
                result['new_count'] += source_new
                result['duplicate_count'] += source_duplicates
                result['excluded_count'] += source_excluded
                source_state.update(seen_ids=sorted(seen_ids), parser_warnings=parser_warnings)
                cursor_sources[source] = source_state
            except SourceError as exc:
                diagnostic.update(status=exc.status, message=exc.message)
                for area in area_specs:
                    state = area_states[area['key']]
                    area_rows[(area['key'], source)] = dict(
                        area_code=area['area_code'], area_name=area['area_name'], source=source,
                        status=exc.status, returned_count=state['returned_count'],
                        total_declared=state['total_declared'], message=exc.message, completed=False,
                    )
                cursor_sources[source] = source_state
            except (ValueError, TypeError, KeyError, ImportError):
                diagnostic.update(status='failed', message='來源格式或依賴異常，請管理者檢查。')
                for area in area_specs:
                    state = area_states[area['key']]
                    area_rows[(area['key'], source)] = dict(
                        area_code=area['area_code'], area_name=area['area_name'], source=source,
                        status='failed', returned_count=state['returned_count'],
                        total_declared=state['total_declared'], message=diagnostic['message'], completed=False,
                    )
                cursor_sources[source] = source_state
            diagnostic['returned_count'] = len(rows)
            diagnostic['new_count'] = source_new
            diagnostic['duplicate_count'] = source_duplicates
            diagnostic['excluded_count'] = source_excluded
            result['source_results'].append(diagnostic)

        if include_keys and company_candidates > 0 and continuation is None:
            result['filter_warnings'].append(
                f'來源回傳{company_candidates}筆，公司名稱符合{company_matches}筆；其餘是標題或內文提到該名稱的其他公司職缺，已略過。')
        result['area_results'] = [area_rows[(area['key'], source)] for area in area_specs for source in arguments.sources]
        result['can_continue'] = any(
            area['has_next'] or area['remaining']
            for state in cursor_sources.values() for area in state['areas'].values())
        if result['can_continue']:
            result['continuation'] = _cursor_result(arguments, cursor_sources)
        return result

    async def detail(self, job, *, deadline=None, finish_reserve=0):
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
            budget = deadline - time.monotonic() - finish_reserve if deadline is not None else None
            if budget is not None and budget < SINGLE_AREA_SECONDS + STARTUP_SECONDS + WORKER_GRACE_SECONDS:
                raise SourceError('failed', '時間不足，未讀取 1111 詳細內容。')
            try:
                payload = await self.browser1111.call(dict(action='detail', job=dict(
                    id=job.source_job_id, title=job.title, company=job.company_name, url=url,
                    companyUrl=f'https://www.1111.com.tw/corp/{job.company_source_id}/')),
                    **({'timeout': budget} if budget is not None else {}))
            except BrowserError as exc:
                if exc.status == 'time_limited':
                    raise SourceError('failed', '時間不足，未讀取 1111 詳細內容。') from None
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
