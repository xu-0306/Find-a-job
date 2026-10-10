"""On-demand evidence with per-source successful-result caching."""
import asyncio
import hashlib
import json
import re
import unicodedata
import statistics
import time
from datetime import timedelta
from urllib.parse import quote

from pydantic import Field
from typing import Literal
from .store import Input, now
from .sources import SourceError
from . import review_parse


# Observed 2026-10-05 public pay-as-you-go rates, without free monthly credits.
PRICING = {'observed_at': '2026-10-05', 'Brave': {'usd_per_1000': 5, 'url': 'https://brave.com/search/api/'},
           'Tavily': {'usd_per_1000': 8, 'url': 'https://docs.tavily.com/documentation/api-credits'}}
BASIC_SOURCES = ('104', 'GoodJob')
BASIC_DEADLINE_SECONDS = 30
REVIEW_STATUSES = {'success', 'partial', 'empty', 'failed', 'ambiguous', 'unsupported'}


def active_provider(store, db):
    provider = store.get(db, 'search_provider', 'auto')
    if provider != 'auto':
        return provider
    return next((name for name in ('Tavily', 'Brave') if store.get(db, name.lower() + '_api_key')), None)


class Assessment(Input):
    provider: Literal['Brave', 'Tavily']
    query_id: str
    position: int = Field(ge=1, le=5)
    relevant: bool
    openable: bool
    date_assessable: bool


class SelectionInput(Input):
    comparison_id: str = Field(min_length=1)
    assessments: list[Assessment] = Field(max_length=120)


def comparison_queries(companies):
    if len(companies) != 4 or len({name_key(x) for x in companies}) != 4:
        raise ValueError('比較需要四家不同的真實來源公司。')
    rows = [(f'company-{i+1}', f'"{name}" 公司評價 工作心得 薪資') for i, name in enumerate(companies)]
    for site, prefix in [('dcard.tw', 'dcard'), ('interview.tw', 'interview')]:
        rows.extend((f'{prefix}-{i+1}', f'site:{site} "{name}" 公司評價 工作心得') for i, name in enumerate(companies[:3]))
    rows += [('1111-company-reference', f'site:1111.com.tw/corp "{companies[0]}" 公司評價'),
             ('1111-job-reference', f'site:1111.com.tw/job "{companies[1]}" 職缺')]
    return [{'id': ident, 'query': query} for ident, query in rows]


def select_provider(comparison, assessments):
    expected = {(row['provider'], row['query_id'], i + 1) for row in comparison['results'] for i in range(len(row['evidence']))}
    supplied = {(x.provider, x.query_id, x.position) for x in assessments}
    if len(supplied) != len(assessments) or supplied != expected:
        raise ValueError('請逐筆確認所有比較結果；不能重複或加入未知項目。')
    scores = []
    for provider in ('Brave', 'Tavily'):
        qualified = [x for x in assessments if x.provider == provider and x.relevant and x.openable]
        latencies = [x['duration_ms'] for x in comparison['results'] if x['provider'] == provider and x['status'] in ('success', 'partial', 'empty')]
        scores.append(dict(provider=provider, qualified_queries=len({x.query_id for x in qualified}), qualified_results=len(qualified),
                           date_assessable=sum(x.date_assessable for x in qualified), usd_per_1000=comparison['pricing'][provider]['usd_per_1000'],
                           median_ms=statistics.median(latencies) if latencies else None))
    ranked = sorted(scores, key=lambda x: (-x['qualified_queries'], -x['qualified_results'], x['usd_per_1000'], x['median_ms'] if x['median_ms'] is not None else float('inf')))
    return (ranked[0]['provider'] if ranked[0]['qualified_queries'] else None), scores


class ReviewInput(Input):
    company_name: str = Field(min_length=1, max_length=500)
    job_id: str | None = Field(default=None, min_length=1, max_length=100)
    source: Literal['104', 'GoodJob', 'Qollie'] | None = None
    company_id: str | None = Field(default=None, min_length=1, max_length=200)
    scope: Literal['basic', 'full'] = 'full'
    refresh: bool = False

    def model_post_init(self, context):
        if bool(self.source) != bool(self.company_id):
            raise ValueError('公司選擇須包含來源及ID')


def name_key(name):
    return re.sub(r'[\s()（）.．,，:：·&_-]+', '', unicodedata.normalize('NFKC', name).casefold())


def cache_key(company, source, identity):
    return 'review-cache/' + hashlib.sha256(json.dumps([company, source, identity], ensure_ascii=False).encode()).hexdigest()


GOODJOB_SEARCH = 'query SearchCompanies($query:String!,$hasData:Boolean!,$start:Int,$limit:Int){search_companies(query:$query,hasData:$hasData,start:$start,limit:$limit){id name}}'
GOODJOB_COMPANY = 'query Company($name:String!){company(name:$name){id name companyRatingStatistics{averageRating ratingCount} workExperiencesResult(start:0,limit:5){count workExperiences{id title created_at}} interviewExperiencesResult(start:0,limit:5){count interviewExperiences{id title created_at}}}}'
QOLLIE_SEARCH = 'query SearchCompanies($kind:String,$keyword:String,$page:Int,$limit:Int){searchCompanies(query:{kind:$kind,keyword:$keyword,limit:$limit,page:$page}){uriHash _id name introduction commentsSize summary{interview_experience{amount} work_experience{amount}}}}'


class Reviews:
    def __init__(self, store, sources):
        self.store, self.sources = store, sources

    async def json(self, method, url, **kwargs):
        response = await self.sources.request(method, url, **kwargs)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get('errors'):
                raise SourceError()
            return payload
        except ValueError:
            raise SourceError() from None

    def match(self, rows, source, body, id_field):
        if not isinstance(rows, list) or any(not isinstance(x, dict) or not x.get('name') or not x.get(id_field) for x in rows):
            raise SourceError()
        if body.source == source:
            matches = [x for x in rows if str(x[id_field]) == body.company_id and name_key(x['name']) == name_key(body.company_name)]
        else:
            matches = [x for x in rows if name_key(x['name']) == name_key(body.company_name)]
        if len(matches) == 1:
            return matches[0], []
        choices = [dict(source=source, company_id=str(x[id_field]), company_name=x['name']) for x in rows[:10]]
        return None, choices

    async def resolve(self, source, body, company104):
        name = body.company_name
        if source == '104':
            headers = {'Origin': 'https://reviews.104.com.tw', 'Referer': 'https://reviews.104.com.tw/'}
            if company104:
                return {'custno': company104, 'name': name}, []
            else:
                payload = await self.json('GET', 'https://be.reviews.104.com.tw/api/companies/autocomplete', params={'keyword': name}, headers=headers)
                return self.match(payload.get('response'), source, body, 'custno')
        if source == 'GoodJob':
            url = 'https://api.goodjob.life/graphql'
            payload = await self.json('POST', url, json={'query': GOODJOB_SEARCH, 'variables': {'query': name, 'hasData': True, 'start': 0, 'limit': 10}})
            return self.match(payload.get('data', {}).get('search_companies'), source, body, 'id')
        payload = await self.json('POST', 'https://www.qollie.com/graphql', json={'query': QOLLIE_SEARCH, 'variables': {'kind': 'company', 'keyword': name, 'page': 1, 'limit': 10}})
        rows = payload.get('data', {}).get('searchCompanies')
        if isinstance(rows, list):
            rows = [dict(x, identifier=x.get('uriHash') or x.get('_id')) for x in rows if isinstance(x, dict)]
        return self.match(rows, source, body, 'identifier')

    async def source(self, source, body, match, *, fetched_at=None):
        name = body.company_name
        if source == '104':
            company_id = str(match['custno'])
            headers = {'Origin': 'https://reviews.104.com.tw', 'Referer': 'https://reviews.104.com.tw/'}
            payload = await self.json(
                'GET',
                'https://be.reviews.104.com.tw/api/reviews',
                params={'custno': company_id, 'page': 1, 'perPage': 5},
                headers=headers,
            )
            info = None
            root = payload.get('response') if isinstance(payload, dict) else None
            has_reviews = (
                bool(root) if isinstance(root, list) else
                isinstance(root, dict)
                and (bool(root.get('items')) or (type(root.get('total')) is int and root['total'] > 0))
            )
            if has_reviews:
                try:
                    info = await self.json(
                        'GET',
                        'https://be.reviews.104.com.tw/api/companies/' + quote(company_id, safe='') + '/info',
                        headers=headers,
                    )
                except SourceError:
                    # The review list remains useful when the optional aggregate endpoint is unavailable.
                    pass
            return review_parse.parse_104(
                payload,
                company_id,
                info_payload=info,
                fetched_at=fetched_at,
                company_name=name,
            )
        if source == 'GoodJob':
            url = 'https://api.goodjob.life/graphql'
            payload = await self.json('POST', url, json={'query': GOODJOB_COMPANY, 'variables': {'name': match['name']}})
            company = payload.get('data', {}).get('company', {})
            if name_key(company.get('name', '')) != name_key(name) or str(company.get('id')) != str(match['id']):
                raise SourceError('blocked', '公司名稱不一致，未合併評價。')
            return review_parse.parse_goodjob(payload, name, company_id=match['id'], fetched_at=fetched_at)
        if source == 'Qollie':
            return review_parse.parse_qollie(match)
        if source == 'PTT':
            response = await self.sources.request('GET', 'https://www.ptt.cc/bbs/Tech_Job/search', params={'q': name})
            parsed = review_parse.parse_ptt(response.text)
            parsed['limitations'].append('PTT 為公開搜尋候選，請核對是否談論同一家公司；未推測年份。')
            return parsed
        with self.store.connect() as db:
            provider = active_provider(self.store, db)
            key = self.store.get(db, (provider or '').lower() + '_api_key', '')
        if provider not in ('Brave', 'Tavily') or not key:
            raise SourceError('unsupported', '搜尋引擎或金鑰尚未設定。')
        return await self.web_search(provider, key, f'"{name}" 公司評價 工作心得')

    async def web_search(self, provider, key, query):
        if provider == 'Brave':
            payload = await self.json('GET', 'https://api.search.brave.com/res/v1/web/search', params={'q': query, 'count': 5, 'country': 'TW', 'search_lang': 'zh-hant'}, headers={'X-Subscription-Token': key, 'Accept': 'application/json'})
        else:
            payload = await self.json('POST', 'https://api.tavily.com/search', json={'query': query, 'search_depth': 'basic', 'max_results': 5, 'include_answer': False, 'include_raw_content': False}, headers={'Authorization': 'Bearer ' + key})
        return review_parse.parse_search(provider, payload)

    async def compare(self, companies, keys):
        from uuid import uuid4
        result = {'id': str(uuid4()), 'at': now().isoformat(), 'queries': comparison_queries(companies), 'company_names': companies,
                  'pricing': PRICING, 'results': [], 'selection': None, 'status': 'pending_assessment'}
        for provider in ('Brave', 'Tavily'):
            for query in result['queries']:
                started = time.monotonic()
                try:
                    if not keys.get(provider): raise SourceError('unsupported', '尚未設定供應商金鑰。')
                    parsed = await self.web_search(provider, keys[provider], query['query'])
                except SourceError as exc:
                    parsed = dict(status=exc.status, evidence=[], limitations=[exc.message])
                parsed.update(provider=provider, query_id=query['id'], duration_ms=round((time.monotonic()-started)*1000))
                result['results'].append(parsed)
        return result

    def _new_result(self, body):
        return dict(
            company_name=body.company_name,
            scope=getattr(body, 'scope', 'full'),
            status='empty',
            summary=None,
            evidence=[],
            ratings=[],
            unavailable_sources=[],
            source_results=[],
            choices=[],
            limitations=[],
            cached=False,
        )

    def _cache_update(self, source, body, parsed, key=None, from_cache=False):
        """Return a persistence tuple for a source result, if its cache policy allows it."""
        if from_cache:
            return None
        status = parsed.get('status')
        cacheable = status in ('success', 'empty')
        if source == 'GoodJob' and status == 'partial' and (parsed.get('evidence') or parsed.get('ratings')):
            cacheable = True
        if not cacheable or key is None:
            return None
        return key, parsed, (now() + timedelta(hours=24)).isoformat()

    async def _fetch_one(self, source, body, company104=None):
        """Resolve, read/cache, and fetch one source without changing the API shape."""

        parsed = None
        from_cache = False
        match = None
        key = None
        try:
            with self.store.connect() as db:
                provider = active_provider(self.store, db)
            identity = provider if source == 'Web' else None
            if source in ('104', 'GoodJob', 'Qollie'):
                match, choices = await self.resolve(source, body, company104)
                if match:
                    identity = str(match[{'104': 'custno', 'GoodJob': 'id', 'Qollie': 'identifier'}[source]])
                else:
                    parsed = dict(status='ambiguous' if choices else 'empty', evidence=[], ratings=[], limitations=[], choices=choices)
            key = cache_key(name_key(body.company_name), source, identity)
            with self.store.connect() as db:
                cached = db.execute(
                    'SELECT payload_json FROM review_cache WHERE company_key=? AND expires_at>?',
                    (key, now().isoformat()),
                ).fetchone()
            if cached and not body.refresh and (parsed is None or parsed['status'] == 'empty'):
                parsed = json.loads(cached[0])
                from_cache = True
            if parsed is None:
                parsed = await self.source(source, body, match, fetched_at=now().isoformat())
            parsed.setdefault('evidence', [])
            parsed.setdefault('ratings', [])
            parsed.setdefault('limitations', [])
            update = self._cache_update(source, body, parsed, key, from_cache)
            return source, parsed, update, from_cache
        except SourceError as exc:
            status = exc.status if exc.status in REVIEW_STATUSES else 'failed'
            failed = dict(status=status, evidence=[], ratings=[], limitations=[exc.message])
            return source, failed, self._cache_update(source, body, failed, key), False
        except asyncio.TimeoutError:
            failed = dict(status='failed', evidence=[], ratings=[], limitations=['來源查詢逾時。'])
            return source, failed, self._cache_update(source, body, failed, key), False
        except (ValueError, TypeError, KeyError, AttributeError):
            failed = dict(status='failed', evidence=[], ratings=[], limitations=['來源格式已變更或查詢未完成。'])
            return source, failed, self._cache_update(source, body, failed, key), False

    def _merge(self, result, source, parsed, from_cache):
        status = parsed.get('status', 'failed')
        if status not in REVIEW_STATUSES:
            status = 'failed'
        source_result = {'source': source, 'status': status}
        if parsed.get('limitations'):
            source_result['message'] = parsed['limitations'][0]
        result['source_results'].append(source_result)
        result['evidence'].extend(parsed.get('evidence', []))
        result['ratings'].extend(parsed.get('ratings', []))
        result['choices'].extend(parsed.get('choices', []))
        result['limitations'].extend(parsed.get('limitations', []))
        if from_cache:
            result['cached'] = True
        if status not in ('success', 'empty'):
            result['unavailable_sources'].append(source)

    def _finish(self, result):
        has_data = bool(result['evidence'] or result['ratings'])
        if has_data:
            result['status'] = 'partial' if result['unavailable_sources'] else 'success'
        elif result['choices']:
            result['status'] = 'ambiguous'
        elif result['unavailable_sources']:
            result['status'] = 'failed'
        else:
            result['status'] = 'empty'
        result['summary'] = (
            '以下為來源提供的評分、摘錄或資料入口，請逐項核對；未平均不同來源，也未產生未經證據支持的結論。'
            if has_data else None
        )
        return result

    async def _get_basic(self, body, company104):
        result = self._new_result(body)
        tasks = [asyncio.create_task(self._fetch_one(source, body, company104)) for source in BASIC_SOURCES]
        try:
            done, pending = await asyncio.wait(tasks, timeout=BASIC_DEADLINE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            updates = []
            for source, task in zip(BASIC_SOURCES, tasks):
                if task in done or (task.done() and not task.cancelled()):
                    try:
                        actual_source, parsed, update, from_cache = task.result()
                    except asyncio.CancelledError:
                        actual_source, parsed, update, from_cache = source, dict(
                            status='failed', evidence=[], ratings=[], limitations=['來源查詢未完成。']
                        ), None, False
                    except Exception:
                        actual_source, parsed, update, from_cache = source, dict(
                            status='failed', evidence=[], ratings=[], limitations=['來源查詢未完成。']
                        ), None, False
                else:
                    actual_source, parsed, from_cache = source, dict(
                        status='failed', evidence=[], ratings=[], limitations=['基本評價查詢逾時，已保留其他來源結果。']
                    ), False
                    update = None
                self._merge(result, actual_source, parsed, from_cache)
                if update:
                    updates.append(update)
            return self._finish(result), updates
        finally:
            # A caller may cancel while waiting for the deadline or draining pending
            # sources.  Do not leave paid/network tasks running in the background.
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    async def get(self, body, company104=None):
        if getattr(body, 'scope', 'full') == 'basic':
            return await self._get_basic(body, company104)
        result = self._new_result(body)
        updates = []
        for source in ('104', 'GoodJob', 'Qollie', 'PTT', 'Web'):
            actual_source, parsed, update, from_cache = await self._fetch_one(source, body, company104)
            self._merge(result, actual_source, parsed, from_cache)
            if update:
                updates.append(update)
        return self._finish(result), updates
