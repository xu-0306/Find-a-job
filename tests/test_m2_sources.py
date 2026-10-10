import asyncio
import json
import re
import unittest
from unittest.mock import patch
import httpx
from app.sources import Sources, SearchInput, SourceError, _valid_518_cursor, region_map
from app.store import Job
from test_m2_source518 import SEARCH_HTML


def search(sources=None, **criteria):
    limit = criteria.pop('limit', 2)
    return SearchInput(criteria={'keyword': '採購', **criteria}, sources=sources or ['104', '518'], limit=limit)


async def fake104(name, args):
    return {'jobs': [{'job_no': '12', 'job_name': '採購', 'company': '合成公司', 'url': 'https://www.104.com.tw/job/a12', 'appear_date': '20261005'}],
            'total': 1, 'has_next': False}, None


class SourceChecks(unittest.IsolatedAsyncioTestCase):
    def test_chat_search_default_limit_is_twenty(self):
        self.assertEqual(SearchInput(criteria={'keyword': '採購'}).limit, 20)

    async def test_two_sources_date_unknown_salary_and_drift(self):
        seen = []
        def transport(req):
            seen.append(str(req.url)); return httpx.Response(200, text=SEARCH_HTML)
        service = Sources(httpx.MockTransport(transport), fake104, interval=0)
        result = await service.search(search(min_monthly_salary=40000))
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(all(x.published_at is None for x in result['items']))
        self.assertTrue(all(x.filter_warnings for x in result['items']))
        self.assertEqual(result['source_results'][1]['status'], 'partial')
        self.assertEqual(len(seen), 1)  # next URL changed aa, so never followed

    async def test_retry_and_redirect_every_hop_guard(self):
        statuses = [429, 302]
        calls = []
        def transport(req):
            calls.append(str(req.url))
            return httpx.Response(statuses.pop(0), headers={'Location': 'https://evil.invalid/steal'})
        service = Sources(httpx.MockTransport(transport), interval=0)
        with self.assertRaises(SourceError): await service.request('GET', 'https://www.518.com.tw/job-index-P-1.html')
        self.assertEqual(len(calls), 2)
        calls.clear()
        service.transport = httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(403))
        with self.assertRaises(SourceError): await service.request('GET', 'https://www.518.com.tw/job-index-P-1.html')
        self.assertEqual(len(calls), 1)

    async def test_retry_budget_can_disable_retries(self):
        calls = []
        service = Sources(httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(429)), interval=0)
        with self.assertRaises(SourceError):
            await service.request('GET', 'https://www.518.com.tw/job-index-P-1.html', max_retries=0)
        self.assertEqual(len(calls), 1)

    async def test_unknown_region_is_not_national_fallback(self):
        service = Sources(httpx.MockTransport(lambda _: self.fail('No network expected')), fake104, interval=0)
        with patch('app.sources.area_catalog', return_value=[{'code': '1', 'name': '合成市'}]):
            result = await service.search(search(area_codes=['999']))
        self.assertFalse(result['items'])
        self.assertTrue(all(x['status'] == 'unsupported' for x in result['source_results']))

    async def test_split_city_catalog_and_ambiguous_suffix(self):
        js = 'times_location:{3001008:"合成市",3001009:"合成市"},sub_location:{3001008001:"北區",3001009001:"南區"}'
        with patch('app.sources.area_catalog', return_value=[{'code': '1', 'name': '合成市'}, {'code': '2', 'name': '合成市北區'}]):
            mapping = region_map(js)
        self.assertEqual(mapping['合成市'], '3001008,3001009')
        self.assertEqual(mapping['合成市北區'], '3001008001')
        with self.assertRaises(SourceError): region_map('changed layout')

    async def test_518_auxiliary_ai_marker_keeps_area_filter_guard(self):
        base = 'https://www.518.com.tw/job-index-P-2.html'
        self.assertTrue(_valid_518_cursor(base + '?ad=%E6%8E%A1%E8%B3%BC&ai=1', 2, '採購', ''))
        self.assertTrue(_valid_518_cursor(base + '?ad=%E6%8E%A1%E8%B3%BC&aa=3001008&ai=1', 2, '採購', '3001008'))
        self.assertFalse(_valid_518_cursor(base + '?ad=%E6%8E%A1%E8%B3%BC&aa=3001009&ai=1', 2, '採購', '3001008'))
        self.assertFalse(_valid_518_cursor(base + '?ad=%E6%8E%A1%E8%B3%BC&ai=2', 2, '採購', '', base + '?ad=%E6%8E%A1%E8%B3%BC&ai=1'))
        self.assertFalse(_valid_518_cursor(base + '?ad=%E6%8E%A1%E8%B3%BC&ai=x', 2, '採購', ''))

    async def test_partial_pages_and_cap(self):
        calls = []
        async def provider(name, args):
            calls.append(args['page'])
            if args['page'] == 2: raise SourceError('blocked')
            return {'jobs': [{'job_no': '12', 'job_name': '職缺', 'company': '公司', 'url': 'https://www.104.com.tw/job/a12'}], 'total': 5, 'has_next': True}, None
        result = await Sources(mcp_call=provider).search(search(['104']))
        self.assertEqual(result['source_results'][0]['status'], 'partial')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(calls, [1, 2])

    async def test_synchronous_cancellation_waits_before_releasing_owner(self):
        import threading
        started, finished = threading.Event(), threading.Event()
        def bounded(name, args, cancelled):
            started.set(); cancelled.wait(2); finished.set(); return {}, None
        with patch('app.sources.mcp_once', side_effect=bounded):
            task = asyncio.create_task(Sources().call104('search_jobs', {}))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(finished.is_set())

    async def test_continuation_counts_duplicates_and_advances_pages(self):
        calls = []

        async def provider(name, args):
            calls.append(args['page'])
            pages = {
                1: [('1', '一'), ('2', '二')],
                2: [('2', '二'), ('3', '三')],
                3: [('4', '四'), ('5', '五')],
            }
            jobs = [dict(job_no=ident, job_name=title, company='公司',
                         url=f'https://www.104.com.tw/job/u{ident}')
                    for ident, title in pages[args['page']]]
            return {'jobs': jobs, 'total': 6, 'has_next': args['page'] < 3}, None

        first = await Sources(mcp_call=provider).search(search(['104']))
        self.assertEqual([job.source_job_id for job in first['items']], ['1', '2'])
        self.assertEqual((first['new_count'], first['duplicate_count']), (2, 0))
        self.assertTrue(first['can_continue'])
        cursor = first['continuation']
        self.assertIsInstance(cursor, str)
        self.assertEqual(json.loads(cursor)['sources']['104']['next_page'], 2)

        service = Sources(mcp_call=provider)
        second = await service.search(search(['104']), continuation=cursor)
        self.assertEqual([job.source_job_id for job in second['items']], ['3', '4'])
        self.assertEqual((second['new_count'], second['duplicate_count']), (2, 1))
        self.assertTrue(second['can_continue'])
        third = await service.search(search(['104']), continuation=second['continuation'])
        self.assertEqual([job.source_job_id for job in third['items']], ['5'])
        self.assertEqual((third['new_count'], third['duplicate_count']), (1, 0))
        self.assertFalse(third['can_continue'])
        self.assertEqual(calls, [1, 2, 3])

    async def test_continuation_keeps_page_overflow_and_excludes_source_key(self):
        calls = []

        async def provider(name, args):
            calls.append(args['page'])
            rows = [
                dict(job_no='1', job_name='一', company='公司', url='https://www.104.com.tw/job/u1'),
                dict(job_no='2', job_name='二', company='公司', url='https://www.104.com.tw/job/u2'),
                dict(job_no='3', job_name='三', company='公司', url='https://www.104.com.tw/job/u3'),
            ] if args['page'] == 1 else []
            return {'jobs': rows, 'total': 3, 'has_next': False}, None

        service = Sources(mcp_call=provider)
        first = await service.search(search(['104'], limit=1), exclude_ids={'104:1'})
        self.assertEqual([job.source_job_id for job in first['items']], ['2'])
        self.assertEqual(first['excluded_count'], 1)
        state = json.loads(first['continuation'])
        self.assertEqual([row['source_job_id'] for row in state['sources']['104']['remaining']], ['3'])
        self.assertEqual(state['sources']['104']['seen_ids'], ['1', '2', '3'])
        second = await service.search(search(['104'], limit=1), continuation=first['continuation'], exclude_ids={'104:1'})
        self.assertEqual([job.source_job_id for job in second['items']], ['3'])
        self.assertFalse(second['can_continue'])
        self.assertEqual(calls, [1])

    async def test_continuation_rejects_changed_page_size_before_network(self):
        calls = []

        async def provider(name, args):
            calls.append(args)
            ident = str(args['page'])
            return {'jobs': [dict(job_no=ident, job_name='一' + ident, company='公司', url=f'https://www.104.com.tw/job/u{ident}')],
                    'total': 2, 'has_next': True}, None

        service = Sources(mcp_call=provider)
        first = await service.search(search(['104']))
        calls_before = len(calls)
        changed = SearchInput(criteria={'keyword': '採購'}, sources=['104'], limit=1)
        result = await service.search(changed, continuation=first['continuation'])
        self.assertEqual(result['source_results'][0]['status'], 'unsupported')
        self.assertEqual(len(calls), calls_before)

    async def test_exhausted_518_is_not_blocked_when_other_source_continues(self):
        requests518 = []
        no_next = re.sub(r'<link rel="next"[^>]*>', '', SEARCH_HTML)

        def transport(req):
            requests518.append(str(req.url)); return httpx.Response(200, text=no_next)

        async def provider(name, args):
            ident = str(args['page'])
            return {'jobs': [dict(job_no=ident, job_name='採購' + ident, company='公司', url=f'https://www.104.com.tw/job/u{ident}')],
                    'total': 2, 'has_next': args['page'] < 2}, None

        service = Sources(httpx.MockTransport(transport), provider, interval=0)
        first = await service.search(search(limit=1))
        self.assertEqual(len(requests518), 1)
        self.assertTrue(first['can_continue'])
        second = await service.search(search(limit=1), continuation=first['continuation'])
        by_source = {x['source']: x for x in second['source_results']}
        self.assertNotEqual(by_source['518']['status'], 'blocked')
        self.assertEqual(len(requests518), 1)
        self.assertEqual([job.source_job_id for job in second['items'] if job.source == '104'], ['2'])
        self.assertFalse(second['can_continue'])

    async def test_tampered_518_cursor_url_is_still_blocked_before_network(self):
        requests518 = []
        linked = SEARCH_HTML.replace('&amp;aa=3001008', '')

        def transport(req):
            requests518.append(str(req.url)); return httpx.Response(200, text=linked)

        service = Sources(httpx.MockTransport(transport), interval=0)
        first = await service.search(search(['518'], limit=1))
        state = json.loads(first['continuation'])
        self.assertTrue(state['sources']['518']['next_url'])
        state['sources']['518']['next_url'] = 'https://www.518.com.tw/job-index-P-2.html?ad=other'
        calls = len(requests518)
        second = await service.search(search(['518'], limit=1), continuation=json.dumps(state, ensure_ascii=False))
        self.assertEqual(second['source_results'][0]['status'], 'blocked')
        self.assertEqual(len(requests518), calls)

    async def test_repeated_104_page_stops_even_when_provider_keeps_has_next(self):
        calls = []

        async def provider(name, args):
            calls.append(args['page'])
            return {'jobs': [dict(job_no='same', job_name='同一筆', company='公司',
                                  url='https://www.104.com.tw/job/same')],
                    'total': 99, 'has_next': True}, None

        result = await Sources(mcp_call=provider).search(search(['104']))
        self.assertEqual([job.source_job_id for job in result['items']], ['same'])
        self.assertFalse(result['can_continue'])
        self.assertEqual(calls, [1, 2])
        self.assertEqual(result['source_results'][0]['parser_warning_count'], 1)

    async def test_detail_does_not_verify_104_from_matching_fields_only(self):
        job = Job(source='104', source_job_id='14561831', title='採購管理師', company_name='範例公司',
                  url='https://www.104.com.tw/job/8o3zb')

        async def detail(name, args):
            self.assertEqual(args, {'detail_id': '8o3zb'})
            return {'job_name': '採購管理師', 'company': '範例公司', 'description': '職務內容',
                    'location': '台南市', 'salary': '月薪 40,000 元'}, None

        result = await Sources(mcp_call=detail).detail(job)
        self.assertEqual(result['job_id'], '14561831')
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['detail']['summary'], '職務內容')
        self.assertEqual(result['detail']['location'], '台南市')
        self.assertEqual(result['detail']['salary_text'], '月薪 40,000 元')

    async def test_detail_can_verify_104_raw_analysis_url_when_package_omits_job_number(self):
        job = Job(source='104', source_job_id='14561831', title='採購管理師', company_name='範例公司',
                  url='https://www.104.com.tw/job/8o3zb')

        async def detail(name, args):
            return (
                {'description': '職務內容'},
                {'data': {'header': {'analysisUrl': 'https://www.104.com.tw/jobs/main/analysis/8o3zb'}}},
            )

        result = await Sources(mcp_call=detail).detail(job)
        self.assertTrue(result['identity_verified'])
        self.assertEqual(result['detail']['identity_evidence']['url'], 'https://www.104.com.tw/jobs/main/analysis/8o3zb')

    async def test_detail_does_not_trust_echoed_104_url(self):
        job = Job(source='104', source_job_id='14561831', title='採購管理師', company_name='範例公司',
                  url='https://www.104.com.tw/job/8o3zb')

        async def detail(name, args):
            return {'url': 'https://www.104.com.tw/job/8o3zb', 'description': '職務內容'}, None

        result = await Sources(mcp_call=detail).detail(job)
        self.assertFalse(result['identity_verified'])

    async def test_detail_rejects_mismatched_518_jsonld_even_with_requested_url(self):
        body = '''<script type="application/ld+json">{"@type":"JobPosting","url":"https://www.518.com.tw/job-A1.html","title":"另一職缺","hiringOrganization":{"name":"另一公司"},"description":"內容"}</script>'''
        job = Job(source='518', source_job_id='A1', title='原職缺', company_name='原公司',
                  url='https://www.518.com.tw/job-A1.html')
        service = Sources(httpx.MockTransport(lambda _: httpx.Response(200, text=body)), interval=0)
        result = await service.detail(job)
        self.assertEqual(result['job_id'], 'A1')
        self.assertFalse(result['identity_verified'])
        self.assertTrue(result['detail']['summary'])


if __name__ == '__main__': unittest.main()
