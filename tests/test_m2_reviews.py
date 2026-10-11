import json
import unittest
import asyncio
from collections import Counter
from app import reviews
from app.sources import Sources
from app.store import Job, encode
import httpx
from unittest.mock import patch
import test_m2_chat as fixtures


def review_response(request, *, ambiguous=False, failure=False):
    if 'autocomplete' in request.url.path:
        rows = [{'custno': 'c1', 'name': '合成公司'}]
        if ambiguous: rows.append({'custno': 'c2', 'name': '合成公司'})
        return httpx.Response(200, json={'response': rows})
    if request.url.path == '/api/reviews':
        return httpx.Response(403) if failure else httpx.Response(200, json={'response': {'items': [{'id': 'r1', 'title': '職場紀錄', 'content': '合成摘錄', 'date': None}], 'total': 1}})
    if request.url.host == 'api.goodjob.life':
        body = json.loads(request.content)
        if 'SearchCompanies' in body['query']: return httpx.Response(200, json={'data': {'search_companies': []}})
    if request.url.host == 'www.qollie.com': return httpx.Response(200, json={'data': {'searchCompanies': []}})
    if request.url.host == 'www.ptt.cc': return httpx.Response(200, text='<div class="r-list-container"></div>')
    if 'brave' in request.url.host: return httpx.Response(200, json={'web': {'results': [{'title': '合成公司工作心得', 'url': 'https://example.invalid/review', 'description': '合成摘要'}]}})
    return httpx.Response(200, json={'results': [{'title': '合成公司工作心得', 'url': 'https://example.invalid/review', 'content': '合成摘要'}]})


class ReviewChecks(unittest.TestCase):
    setUp = fixtures.ChatChecks.setUp
    tearDown = fixtures.ChatChecks.tearDown

    def setup_source(self, **kwargs):
        self.calls = Counter()
        def handler(request):
            self.calls[request.url.host + request.url.path] += 1
            return review_response(request, **kwargs)
        self.app.state.sources = Sources(httpx.MockTransport(handler), interval=0)
        self.app.state.reviews = reviews.Reviews(self.store, self.app.state.sources)

    def query(self, **kwargs):
        return self.client.post('/api/reviews', json={'company_name': '合成公司', **kwargs})

    def test_cache_success_only_refresh_and_expiry(self):
        self.setup_source()
        result = self.query().json()
        self.assertEqual(result['status'], 'partial')  # no selected Web provider
        self.assertEqual(result['evidence'][0]['excerpt'], '合成摘錄')
        self.assertIsNone(result['evidence'][0]['date'])
        before = self.calls.copy()
        self.assertTrue(self.query().json()['cached'])
        self.assertEqual(before['be.reviews.104.com.tw/api/reviews'], self.calls['be.reviews.104.com.tw/api/reviews'])
        self.assertEqual(before['www.ptt.cc/bbs/Tech_Job/search'], self.calls['www.ptt.cc/bbs/Tech_Job/search'])
        self.query(refresh=True)
        self.assertGreater(sum(self.calls.values()), sum(before.values()))
        with self.store.connect(write=True) as db: db.execute("UPDATE review_cache SET expires_at='2000-01-01T00:00:00+00:00'")
        count = sum(self.calls.values()); self.query()
        self.assertGreater(sum(self.calls.values()), count)

    def test_basic_scope_uses_only_free_sources_and_ignores_legacy_interview_cache(self):
        self.calls = Counter()

        def handler(request):
            self.calls[request.url.host + request.url.path] += 1
            if request.url.host == 'be.reviews.104.com.tw':
                if request.url.path.endswith('/autocomplete'):
                    return httpx.Response(200, json={'response': [{'custno': 'c1', 'name': '合成公司'}]})
                if request.url.path.endswith('/info'):
                    return httpx.Response(200, json={'response': {
                        'custno': 'c1', 'companyName': '合成公司', 'reviewCount': 2,
                        'scoreOverall': 3.5, 'interviewCount': 1, 'interviewScore': 4,
                    }})
                return httpx.Response(200, json={'response': {'items': [
                    {'id': 'r104', 'title': '104 摘錄', 'content': '104 內容'}
                ], 'total': 1}})
            if request.url.host == 'api.goodjob.life':
                query = json.loads(request.content)['query']
                if 'search_companies' in query:
                    return httpx.Response(200, json={'data': {'search_companies': [{'id': 'g1', 'name': '合成公司'}]}})
                return httpx.Response(200, json={'data': {'company': {
                    'id': 'g1', 'name': '合成公司',
                    'companyRatingStatistics': {'averageRating': 4.25, 'ratingCount': 12},
                    'workExperiencesResult': {'count': 1, 'workExperiences': [
                        {'id': 'w1', 'title': 'GoodJob 摘錄', 'experience': 'GoodJob 內容'}
                    ]},
                    'interviewExperiencesResult': {'count': 0, 'interviewExperiences': []},
                }}})
            return httpx.Response(200, json={'results': []})

        self.app.state.sources = Sources(httpx.MockTransport(handler), interval=0)
        self.app.state.reviews = reviews.Reviews(self.store, self.app.state.sources)
        with self.store.connect(write=True) as db:
            self.store.put(db, 'tavily_api_key', 'synthetic-tavily-secret')
            # Rows left by the removed Interview.tw source must not be read or break the query.
            legacy = reviews.cache_key(reviews.name_key('合成公司'), 'Interview.tw', 'interview.tw')
            db.execute('INSERT INTO review_cache VALUES(?,?,?)', (legacy, json.dumps({'status': 'failed', 'evidence': [], 'ratings': []}), '2999-01-01T00:00:00+00:00'))
        first = self.query(scope='basic').json()
        self.assertEqual(first['scope'], 'basic')
        self.assertEqual(first['status'], 'success')
        self.assertEqual({row['source'] for row in first['source_results']}, {'104', 'GoodJob'})
        self.assertEqual(len(first['ratings']), 3)
        self.assertEqual(first['ratings'][0]['sample_count'], 2)
        self.assertEqual(first['ratings'][-1]['source_company_id'], 'g1')
        self.assertTrue(any(row['source'] == '104' for row in first['evidence']))
        self.assertTrue(any(row['source'] == 'GoodJob' for row in first['evidence']))
        self.assertEqual(self.calls['api.tavily.com/search'], 0)
        full = self.query().json()
        self.assertTrue(full['cached'])
        self.assertEqual(len(full['ratings']), 3)
        self.assertTrue(any(row['source'] == '104' for row in full['evidence']))
        self.assertTrue(any(row['source'] == 'GoodJob' for row in full['evidence']))
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/reviews'], 1)
        second = self.query(scope='basic').json()
        self.assertTrue(second['cached'])
        self.assertEqual(len(second['ratings']), 3)
        self.assertTrue(any(row['source'] == '104' for row in second['evidence']))
        self.assertTrue(any(row['source'] == 'GoodJob' for row in second['evidence']))
        self.assertEqual(self.calls['api.tavily.com/search'], 1)  # full's Web search only
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/reviews'], 1)

    def test_104_info_is_skipped_when_review_list_is_empty(self):
        self.calls = Counter()

        def handler(request):
            self.calls[request.url.host + request.url.path] += 1
            if request.url.path.endswith('/autocomplete'):
                return httpx.Response(200, json={'response': [{'custno': 'c1', 'name': '合成公司'}]})
            if request.url.path.endswith('/api/reviews'):
                return httpx.Response(200, json={'response': {'items': [], 'total': 0}})
            if request.url.path.endswith('/info'):
                return httpx.Response(404, json={'error': {'code': 404}})
            return httpx.Response(200, json={'results': []})

        self.app.state.sources = Sources(httpx.MockTransport(handler), interval=0)
        self.app.state.reviews = reviews.Reviews(self.store, self.app.state.sources)
        body = reviews.ReviewInput(company_name='合成公司')
        result = asyncio.run(self.app.state.reviews.source('104', body, {'custno': 'c1', 'name': '合成公司'}))
        self.assertEqual(result['status'], 'empty')
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/reviews'], 1)
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/companies/c1/info'], 0)

    def test_basic_scope_keeps_completed_source_when_deadline_expires(self):
        async def fake_fetch(source, body, company104=None):
            if source == 'GoodJob':
                return source, {'status': 'success', 'evidence': [{'source': source}], 'ratings': [], 'limitations': []}, None, False
            await asyncio.sleep(0.05)

        with patch.object(self.app.state.reviews, '_fetch_one', fake_fetch), patch.object(reviews, 'BASIC_DEADLINE_SECONDS', 0.001):
            result = self.query(scope='basic').json()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['evidence'][0]['source'], 'GoodJob')
        self.assertEqual(result['source_results'][1]['status'], 'success')
        self.assertEqual(result['source_results'][0]['status'], 'failed')

    def test_goodjob_partial_with_verified_data_is_cacheable(self):
        body = reviews.ReviewInput(company_name='合成公司')
        key = reviews.cache_key(reviews.name_key(body.company_name), 'GoodJob', 'g1')
        parsed = {
            'status': 'partial',
            'evidence': [{'source': 'GoodJob', 'title': '摘錄'}],
            'ratings': [{'source': 'GoodJob', 'value': 4.2}],
        }
        update = self.app.state.reviews._cache_update('GoodJob', body, parsed, key)
        self.assertIsNotNone(update)
        self.assertEqual(update[0], key)
        self.assertEqual(update[1]['status'], 'partial')
        failed = {'status': 'failed', 'evidence': [], 'ratings': []}
        self.assertIsNone(self.app.state.reviews._cache_update('GoodJob', body, failed, key))

    def test_basic_cancellation_drains_child_tasks(self):
        started, finished = [], []

        async def fake_fetch(source, body, company104=None):
            started.append(source)
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(source)

        async def cancel_request():
            body = reviews.ReviewInput(company_name='合成公司', scope='basic')
            request = asyncio.create_task(self.app.state.reviews.get(body))
            for _ in range(100):
                if len(started) == len(reviews.BASIC_SOURCES):
                    break
                await asyncio.sleep(0)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.sleep(0)

        with patch.object(self.app.state.reviews, '_fetch_one', fake_fetch):
            asyncio.run(cancel_request())
        self.assertEqual(set(started), set(reviews.BASIC_SOURCES))
        self.assertEqual(set(finished), set(reviews.BASIC_SOURCES))

    def test_failure_not_cached_and_ambiguity_not_merged(self):
        self.setup_source(failure=True)
        self.query(); self.query()
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/reviews'], 2)
        with self.store.connect(write=True) as db: db.execute('DELETE FROM review_cache')
        self.setup_source(ambiguous=True)
        result = self.query().json()
        self.assertEqual(len(result['choices']), 2)
        self.assertEqual(self.calls['be.reviews.104.com.tw/api/reviews'], 0)
        result = self.query(source='104', company_id='c2').json()
        self.assertEqual(len(result['evidence']), 1)

    def test_company_mismatch_and_auth_denied_before_requests(self):
        self.setup_source()
        job = Job(source='104', source_job_id='j', company_name='另一家公司', title='職缺', url='https://www.104.com.tw/job/abc')
        ident = self.store.save_job(job)
        self.assertEqual(self.query(job_id=ident).status_code, 422)
        self.assertFalse(self.calls)
        self.client.cookies.delete('jc_user')
        self.assertEqual(self.query().status_code, 401)
        self.assertFalse(self.calls)

    def test_provider_comparison_exact_twelve_scoring_and_key_invalidation(self):
        from app import security
        self.setup_source()
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin_token)
        with self.store.connect(write=True) as db:
            self.store.put(db, 'brave_api_key', 'synthetic-brave-secret'); self.store.put(db, 'tavily_api_key', 'synthetic-tavily-secret')
        for i in range(4):
            self.store.save_job(Job(source='104', source_job_id=str(i), title='合成', company_name='合成公司'+str(i), url='https://www.104.com.tw/job/a'+str(i)))
        result = self.client.post('/api/admin/search-comparison')
        self.assertEqual(result.status_code, 200)
        comparison = result.json()
        self.assertEqual(len(comparison['queries']), 12)
        self.assertEqual(len(comparison['results']), 24)
        self.assertNotIn('secret', result.text)
        assessments = [dict(provider=row['provider'], query_id=row['query_id'], position=1, relevant=True, openable=True, date_assessable=False) for row in comparison['results']]
        body = {'comparison_id': comparison['id'], 'assessments': assessments}
        self.assertEqual(self.client.post('/api/admin/search-selection', json=body).json()['selection'], 'Brave')
        body['assessments'].append(body['assessments'][0])
        self.assertEqual(self.client.post('/api/admin/search-selection', json=body).status_code, 422)
        self.client.put('/api/admin/settings', json={'brave_api_key': 'new-synthetic-secret'})
        self.assertIsNone(self.client.get('/api/admin/search-comparison').json())

    def test_search_service_automatic_keys_and_manual_choice(self):
        from app import security
        self.setup_source()
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin_token)
        for body, expected in [
            ({'search_provider':'auto'}, None),
            ({'brave_api_key':'synthetic-brave-secret'}, 'Brave'),
            ({'tavily_api_key':'synthetic-tavily-secret'}, 'Tavily'),
            ({'clear_secrets':['tavily_api_key']}, 'Brave'),
            ({'search_provider':'Tavily'}, 'Tavily'),
            ({'search_provider':None}, None),
            ({'search_provider':'auto','clear_secrets':['brave_api_key']}, None),
        ]:
            saved = self.client.put('/api/admin/settings', json=body)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()['active_search_provider'], expected)
        self.assertEqual(self.client.post('/api/admin/search-test').status_code, 422)
        self.assertFalse(self.calls)
        self.client.put('/api/admin/settings', json={'tavily_api_key':'synthetic-tavily-secret'})
        self.assertEqual(self.client.post('/api/admin/search-test').json()['provider'], 'Tavily')
        self.client.headers['X-CSRF-Token'] = security.csrf(self.user_token)
        self.assertEqual(self.query(refresh=True).status_code, 200)
        self.assertEqual(self.calls['api.tavily.com/search'], 2)
        self.assertFalse(self.calls['api.search.brave.com/res/v1/web/search'])

    def test_search_connection_one_query_failure_and_key_change_preserves_provider(self):
        from app import security
        self.setup_source()
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin_token)
        self.assertEqual(self.client.post('/api/admin/search-test').status_code, 422)
        self.assertFalse(self.calls)
        saved = self.client.put('/api/admin/settings', json={'search_provider':'Tavily','tavily_api_key':'synthetic-search-secret'})
        self.assertEqual(saved.status_code, 200)
        result = self.client.post('/api/admin/search-test')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['passed'])
        self.assertEqual(result.json()['result_count'], 1)
        self.assertEqual(sum(self.calls.values()), 1)
        self.assertEqual(self.calls['api.tavily.com/search'], 1)
        self.assertNotIn('secret', result.text)
        self.assertTrue(self.client.get('/api/admin/settings').json()['search_connection_test']['passed'])
        changed = self.client.put('/api/admin/settings', json={'tavily_api_key':'replacement-synthetic-secret'}).json()
        self.assertEqual(changed['search_provider'], 'Tavily')
        self.assertIsNone(changed['search_connection_test'])
        self.client.put('/api/admin/settings', json={'search_provider':'Brave','brave_api_key':'synthetic-brave-secret'})
        self.assertTrue(self.client.post('/api/admin/search-test').json()['passed'])
        self.assertEqual(self.calls['api.search.brave.com/res/v1/web/search'], 1)
        async def malformed(*args): return {'status':'failed','evidence':[]}
        with patch.object(self.app.state.reviews, 'web_search', malformed):
            self.assertEqual(self.client.post('/api/admin/search-test').status_code, 503)
        self.assertFalse(self.client.get('/api/admin/settings').json()['search_connection_test']['passed'])
        self.assertEqual(self.client.put('/api/admin/settings', json={'search_provider':'unsupported'}).status_code, 422)
        self.client.headers['X-CSRF-Token'] = 'bad'
        self.assertEqual(self.client.post('/api/admin/search-test').status_code, 403)
        self.client.cookies.delete('jc_admin')
        self.assertEqual(self.client.post('/api/admin/search-test').status_code, 401)

    def test_search_connection_late_configuration_and_revoked_session_not_saved(self):
        from app import security
        self.client.headers['X-CSRF-Token'] = security.csrf(self.admin_token)
        self.client.put('/api/admin/settings', json={'search_provider':'Tavily','tavily_api_key':'synthetic-search-secret'})
        async def changed(*args):
            with self.store.connect(write=True) as db: self.store.put(db, 'tavily_api_key', 'changed-synthetic-secret')
            return {'status':'empty','evidence':[]}
        with patch.object(self.app.state.reviews, 'web_search', changed):
            self.assertEqual(self.client.post('/api/admin/search-test').status_code, 409)
        self.assertIsNone(self.client.get('/api/admin/settings').json()['search_connection_test'])
        async def revoked(*args):
            with self.store.connect(write=True) as db: db.execute("DELETE FROM sessions WHERE role='admin'")
            return {'status':'empty','evidence':[]}
        with patch.object(self.app.state.reviews, 'web_search', revoked):
            self.assertEqual(self.client.post('/api/admin/search-test').status_code, 401)
        with self.store.connect() as db: self.assertIsNone(self.store.get(db, 'search_test'))


if __name__ == '__main__': unittest.main()
