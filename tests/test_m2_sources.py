import asyncio
import json
import re
import unittest
from unittest.mock import patch
import httpx
from app.job_normalize import company_excluded, company_key
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
    def test_source_mentions_distinguish_links_and_digit_groups(self):
        from app.sources import SearchInput, mentions_source, requested_sources
        for text in ('月薪 52,104', '52，104', '0912-518-123', '02-1111-2222',
                     'https://www.104.com.tw/job/abc123', 'https://www.518.com.tw/job-G123.html',
                     'https://www.1111.com.tw/job/98765', '61,518', '03-104-7777'):
            with self.subTest(text=text): self.assertFalse(mentions_source(text))
        for text in ('只查104', '不要1111', '１１１１', '只看 104.com.tw', '104。',
                     '請用５１８', '只查 https://www.104.com.tw 的職缺',
                     'https://www.104.com.tw/job/abc123，但不要1111'):
            with self.subTest(text=text): self.assertTrue(mentions_source(text))
        arguments = SearchInput(criteria={'keyword': '產品經理'}, sources=['104', '518'])
        enabled = ['104', '518', '1111']
        self.assertEqual(requested_sources(arguments, enabled, ['不要1111', '再找產品經理']), ['104', '518'])
        self.assertEqual(requested_sources(arguments, enabled, ['月薪 52,104', '再找產品經理']), enabled)

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
        self.assertEqual(json.loads(cursor)['sources']['104']['areas']['all']['next_page'], 2)

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
        self.assertEqual([row['source_job_id'] for row in state['sources']['104']['areas']['all']['remaining']], ['3'])
        self.assertEqual(state['sources']['104']['seen_ids'], ['1', '2', '3'])
        second = await service.search(search(['104'], limit=1), continuation=first['continuation'], exclude_ids={'104:1'})
        self.assertEqual([job.source_job_id for job in second['items']], ['3'])
        self.assertFalse(second['can_continue'])
        self.assertEqual(calls, [1])

    async def test_company_exclusion_normalizes_names_and_applies_to_queued_rows(self):
        async def provider(name, args):
            rows = [
                dict(job_no='1', job_name='一', company='工研院_財團法人工業技術研究院', url='https://www.104.com.tw/job/u1'),
                dict(job_no='2', job_name='二', company='光寶科技股份有限公司', url='https://www.104.com.tw/job/u2'),
                dict(job_no='3', job_name='三', company='ＡＤＶＡＮＴＥＣＨ 研華股份有限公司', url='https://www.104.com.tw/job/u3'),
                dict(job_no='4', job_name='四', company='臺灣合成股份有限公司', url='https://www.104.com.tw/job/u4'),
            ] if args['page'] == 1 else []
            return {'jobs': rows, 'total': 4, 'has_next': False}, None

        service = Sources(mcp_call=provider)
        first = await service.search(search(['104'], limit=1), exclude_companies=['工研院', 'advantech'])
        self.assertEqual([job.source_job_id for job in first['items']], ['2'])
        self.assertEqual(first['excluded_count'], 2)
        # A company excluded after the first batch also drops rows already queued in the cursor.
        second = await service.search(search(['104'], limit=1), continuation=first['continuation'],
                                      exclude_companies=['工研院', 'advantech', '台灣合成'])
        self.assertEqual(second['items'], [])
        self.assertEqual(second['excluded_count'], 1)
        self.assertFalse(second['can_continue'])

    def test_company_exclusion_keeps_latin_word_boundaries(self):
        keys = [company_key(name) for name in ('Meta', 'IBM')]
        for name, expected in (('MetalWorks有限公司', False), ('Meta Platforms Taiwan', True), ('META台灣分公司', True),
                               ('ibm-台灣國際商業機器', True), ('IBMX Labs', False), ('Big Meta-data Co', True)):
            with self.subTest(name=name):
                self.assertEqual(company_excluded(name, keys), expected)
        self.assertTrue(company_excluded('台積電股份有限公司', [company_key('台積')]))
        self.assertEqual(company_key('臺 灣 合成 股份有限公司'), company_key('台灣合成'))

    def test_exclude_companies_input_is_bounded(self):
        self.assertEqual(SearchInput(criteria={'keyword': '採購'}, exclude_companies=['工研院', '工研院']).exclude_companies, ['工研院'])
        for names in (['A'], ['x' * 81], ['公司'] * 21, ['股份有限公司']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                SearchInput(criteria={'keyword': '採購'}, exclude_companies=names)

    def test_include_companies_input_is_bounded_and_daily_default_is_empty(self):
        from app.sources import DailySearchInput
        self.assertEqual(SearchInput(criteria={'keyword': '採購'}).include_companies, [])
        self.assertEqual(DailySearchInput(criteria={'keyword': '採購'}).include_companies, [])
        self.assertEqual(DailySearchInput(criteria={'keyword': '採購'}).limit, 100)
        self.assertEqual(SearchInput(criteria={'keyword': '採購'},
            include_companies=['霽禾流光', '雲岑曜石', '霽禾流光']).include_companies, ['霽禾流光', '雲岑曜石'])
        for names in ([''], [' '], ['A'], ['x' * 81], ['霽禾流光'] * 6, ['股份有限公司'], ['A-']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                SearchInput(criteria={'keyword': '採購'}, include_companies=names)
        for names in (['xy'], ['x' * 80], [f'霽禾流光{i}' for i in range(5)]):
            with self.subTest(names=names):
                self.assertEqual(SearchInput(criteria={'keyword': '採購'}, include_companies=names).include_companies, names)

    def test_include_company_schema_and_prompt_concepts(self):
        from app.chat import SYSTEM
        schema = SearchInput.model_json_schema()['properties']['include_companies']
        self.assertEqual(schema['maxItems'], 5)
        self.assertEqual(schema['items']['type'], 'string')
        self.assertNotIn('include_companies', SearchInput.model_json_schema()['required'])
        for concept in ('使用者說法', '公司全名／簡稱', '公司名稱符合', 'criteria.keyword', '沒有符合'):
            self.assertIn(concept, schema['description'])
        self.assertIn('include_companies', SYSTEM)

    async def test_include_companies_matches_names_aliases_and_latin_boundaries_with_exclusions(self):
        companies = ['璇嶼映川科技股份有限公司(沐橋)', '曦沐星橋科技股份有限公司', 'Meta Lumen Labs',
                     'MetalWorks', '別家僱主', '曦沐星橋研究', '曦沐星橋開發']

        async def provider(name, args):
            return dict(jobs=[dict(job_no=str(i), job_name='曦沐星橋現場職缺', company=company, is_ad=False,
                url=f'https://www.104.com.tw/job/u{i}') for i, company in enumerate(companies, 1)],
                total=len(companies), has_next=False), None

        arguments = SearchInput(criteria={'keyword': '曦沐星橋'}, sources=['104'],
                                include_companies=['沐橋', '曦沐星橋', 'meta'])
        result = await Sources(mcp_call=provider).search(arguments,
            exclude_companies=['曦沐星橋研究'], exclude_ids={'104:7'})
        self.assertEqual([job.source_job_id for job in result['items']], ['1', '2', '3'])
        self.assertEqual(result['excluded_count'], 4)
        self.assertEqual(result['source_results'][0]['excluded_count'], 4)
        self.assertEqual(result['filter_warnings'], [
            '來源回傳7筆，公司名稱符合5筆；其餘是標題或內文提到該名稱的其他公司職缺，已略過。'])
        self.assertFalse(result['can_continue'])

    async def test_include_http_pagination_still_fetches_five_pages(self):
        from urllib.parse import urlencode
        for source in ('104', '518'):
            with self.subTest(source=source):
                calls = []
                arguments = SearchInput(criteria={'keyword': '霽禾流光'}, sources=[source],
                                        include_companies=['霽禾流光'])

                async def provider(name, args):
                    number = args['page']; calls.append(number)
                    return dict(jobs=[dict(job_no=str(number), job_name='職缺',
                        company='霽禾流光' if number == 5 else '別家僱主',
                        url=f'https://www.104.com.tw/job/u{number}')], total=100, has_next=True), None

                def transport(request):
                    number = int(re.search(r'-P-(\d+)', request.url.path)[1]); calls.append(number)
                    company = '霽禾流光' if number == 5 else '別家僱主'
                    query = urlencode(dict(ad=arguments.criteria.keyword, aa=''))
                    return httpx.Response(200, text=f'<link rel="next" href="/job-index-P-{number+1}.html?{query}">'
                        f'<div id="listContent"><div class="job__card" data-id="A{number}" data-url="/job-A{number}.html">'
                        f'<a class="job__title" href="/job-A{number}.html">職缺</a>'
                        f'<span class="job__comp__name">{company}</span></div></div>')

                result = await Sources(httpx.MockTransport(transport), mcp_call=provider, interval=0).search(arguments)
                self.assertEqual(calls, [1, 2, 3, 4, 5])
                self.assertEqual([job.company_name for job in result['items']], ['霽禾流光'])
                self.assertEqual(result['excluded_count'], 4)
                self.assertTrue(result['can_continue'])
                self.assertIn('來源回傳5筆，公司名稱符合1筆；其餘是標題或內文提到該名稱的其他公司職缺，已略過。',
                              result['filter_warnings'])

    async def test_include_1111_stops_at_two_pages_and_default_still_stops_at_five(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        for include, count, matches in ((False, 1, True), (True, 1, True), (True, 3, True), (True, 1, False)):
            with self.subTest(include=include, areas=count, matches=matches):
                areas = [dict(code=str(910000 + i), name=f'合成區{i}') for i in range(count)]
                browser = AsyncMock()

                async def worker(args):
                    number = args['page']
                    return dict(status='ok', areas=[dict(page([
                        dict(row(str(100000 + index * 1000 + number * 100 + i)),
                             company='霽禾流光' if matches and i == 0 else '別家僱主') for i in range(30)],
                        number=number, size=30, total=3000), area_name=name)
                        for index, name in enumerate(args['area_names'])])

                browser.call.side_effect = worker
                arguments = SearchInput(criteria={'keyword': '霽禾流光', 'area_codes': [area['code'] for area in areas]},
                    sources=['1111'], include_companies=['霽禾流光'] if include else [])
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await Sources(browser1111=browser).search(arguments, exclude_companies=['別家僱主'])
                self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list],
                                 [1, 2] if include else [1, 2, 3, 4, 5])
                self.assertEqual(len(result['items']), count * int(matches) * (2 if include else 5))
                self.assertEqual(result['excluded_count'], count * (30 - int(matches)) * (2 if include else 5))
                self.assertFalse(result['can_continue'])
                self.assertIsNone(result['continuation'])
                self.assertTrue(all(area['completed'] for area in result['area_results']))
                self.assertEqual(result['filter_warnings'].count('1111指名公司的搜尋只查前2頁，未再翻頁。'), int(include))

    async def test_include_1111_keeps_twenty_fifth_row_before_queue_limit(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        browser = AsyncMock()
        browser.call.return_value = page([dict(row(str(100000 + index)),
            company='霽禾流光' if index == 24 else '別家僱主') for index in range(30)], size=30, total=30)
        result = await Sources(browser1111=browser).search(SearchInput(
            criteria={'keyword': '霽禾流光'}, sources=['1111'], include_companies=['霽禾流光']))
        self.assertEqual([job.source_job_id for job in result['items']], ['100024'])
        self.assertEqual(result['excluded_count'], 29)
        self.assertEqual(browser.call.await_count, 1)
        self.assertFalse(result['can_continue'])
        self.assertIn('來源回傳30筆，公司名稱符合1筆；其餘是標題或內文提到該名稱的其他公司職缺，已略過。',
                      result['filter_warnings'])

    async def test_include_1111_continuation_keeps_overflow_without_third_page(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        browser = AsyncMock()

        async def worker(args):
            number = args['page']
            # Page two has more matching rows than the remaining display limit.
            return page([dict(row(str(100000 + number * 100 + i)), company='霽禾流光')
                         if i < number else dict(row(str(100000 + number * 100 + i)), company='別家僱主')
                         for i in range(30)], number=number, size=30, total=300)

        browser.call.side_effect = worker
        service = Sources(browser1111=browser)
        arguments = SearchInput(criteria={'keyword': '霽禾流光'}, sources=['1111'], limit=1,
                                include_companies=['霽禾流光'])
        first = await service.search(arguments)
        self.assertTrue(first['can_continue'])
        second = await service.search(arguments, continuation=first['continuation'])
        self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list], [1, 2])
        self.assertEqual([job.source_job_id for job in second['items']], ['100200'])
        self.assertTrue(second['can_continue'])
        state = json.loads(second['continuation'])['sources']['1111']['areas']['all']
        self.assertFalse(state['has_next'])
        self.assertEqual([job['source_job_id'] for job in state['remaining']], ['100201'])
        self.assertEqual(second['filter_warnings'], [
            '1111 為實驗來源，可能因人機驗證受阻；地點、薪資未能解析，請至來源核對。',
            '1111指名公司的搜尋只查前2頁，未再翻頁。'])
        third = await service.search(arguments, continuation=second['continuation'])
        self.assertEqual([job.source_job_id for job in third['items']], ['100201'])
        self.assertFalse(third['can_continue'])
        self.assertIsNone(third['continuation'])
        self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list], [1, 2])
        self.assertEqual(sum(result['filter_warnings'].count('1111指名公司的搜尋只查前2頁，未再翻頁。')
                             for result in (first, second, third)), 1)

    async def test_include_1111_page_cap_keeps_http_continuation(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        calls = []

        async def provider(name, args):
            number = args['page']; calls.append(number)
            return dict(jobs=[dict(job_no=str(number), job_name='職缺', company='別家僱主',
                url=f'https://www.104.com.tw/job/u{number}')], total=100, has_next=True), None

        async def worker(args):
            number = args['page']
            return page([dict(row(str(100000 + number * 100 + i)), company='別家僱主')
                         for i in range(30)], number=number, size=30, total=300)

        browser = AsyncMock(); browser.call.side_effect = worker
        result = await Sources(mcp_call=provider, browser1111=browser).search(SearchInput(
            criteria={'keyword': '霽禾流光'}, sources=['104', '1111'], include_companies=['霽禾流光']))
        self.assertEqual(calls, [1, 2, 3, 4, 5])
        self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list], [1, 2])
        self.assertTrue(result['can_continue'])
        states = json.loads(result['continuation'])['sources']
        self.assertEqual({source for source, state in states.items()
                          if any(area['has_next'] for area in state['areas'].values())}, {'104'})
        self.assertFalse(states['1111']['areas']['all']['remaining'])
        self.assertTrue(result['source_results'][1]['truncated'])
        self.assertEqual(result['filter_warnings'].count('1111指名公司的搜尋只查前2頁，未再翻頁。'), 1)

    async def test_include_1111_exactly_two_pages_is_not_truncated(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        browser = AsyncMock()
        browser.call.side_effect = lambda args: page([
            dict(row(str(100000 + args['page'])), company='霽禾流光')], number=args['page'], total=2)
        result = await Sources(browser1111=browser).search(SearchInput(
            criteria={'keyword': '霽禾流光'}, sources=['1111'], include_companies=['霽禾流光']))
        self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list], [1, 2])
        self.assertEqual([job.source_job_id for job in result['items']], ['100001', '100002'])
        self.assertNotIn('1111指名公司的搜尋只查前2頁，未再翻頁。', result['filter_warnings'])
        self.assertFalse(result['source_results'][0]['truncated'])
        self.assertFalse(result['can_continue'])
        self.assertIsNone(result['continuation'])
        self.assertTrue(result['area_results'][0]['completed'])

    async def test_include_1111_failed_area_can_retry_after_other_area_caps(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        areas = [dict(code='910001', name='合成北區'), dict(code='910002', name='合成南區')]
        browser = AsyncMock()

        async def worker(args):
            number = args['page']
            if browser.call.await_count == 1:
                return dict(status='ok', areas=[dict(area_name=areas[0]['name'], status='failed', message='合成查詢失敗'),
                    dict(page([dict(row('200101'), company='別家僱主')], total=3), area_name=areas[1]['name'])])
            return dict(status='ok', areas=[dict(page([
                dict(row(str(100100 + number)), company='別家僱主' if number == 2 else '霽禾流光')],
                number=number, total=3 if number == 2 else 1), area_name=args['area_names'][0])])

        browser.call.side_effect = worker
        service = Sources(browser1111=browser)
        arguments = SearchInput(criteria={'keyword': '霽禾流光', 'area_codes': [area['code'] for area in areas]},
                                sources=['1111'], include_companies=['霽禾流光'])
        with patch('app.sources.area_catalog', return_value=areas):
            first = await service.search(arguments)
            self.assertEqual([area['status'] for area in first['area_results']], ['failed', 'success'])
            self.assertEqual([area['completed'] for area in first['area_results']], [False, True])
            self.assertTrue(first['can_continue'])
            states = json.loads(first['continuation'])['sources']['1111']['areas']
            self.assertTrue(states['910001']['has_next'])
            self.assertEqual(states['910001']['next_page'], 1)
            self.assertFalse(states['910002']['has_next'])
            self.assertEqual(first['filter_warnings'].count('1111指名公司的搜尋只查前2頁，未再翻頁。'), 1)
            second = await service.search(arguments, continuation=first['continuation'])
        self.assertEqual([(call.args[0]['page'], call.args[0]['area_names']) for call in browser.call.await_args_list],
                         [(1, ['合成北區', '合成南區']), (2, ['合成南區']), (1, ['合成北區'])])
        self.assertEqual([job.source_job_id for job in second['items']], ['100101'])
        self.assertFalse(second['can_continue'])
        self.assertIsNone(second['continuation'])
        self.assertTrue(all(area['completed'] for area in second['area_results']))
        self.assertNotIn('1111指名公司的搜尋只查前2頁，未再翻頁。', second['filter_warnings'])

    async def test_include_unavailable_or_empty_sources_have_no_candidate_count(self):
        from unittest.mock import AsyncMock
        from app.source1111 import BrowserError
        from test_source1111 import page
        for source in ('104', '1111'):
            for status in ('blocked', 'failed', 'empty'):
                with self.subTest(source=source, status=status):
                    provider = AsyncMock()
                    browser = AsyncMock()
                    if status == 'empty':
                        provider.return_value = dict(jobs=[], total=0, has_next=False), None
                        browser.call.return_value = page([], total=0)
                    else:
                        provider.side_effect = SourceError(status)
                        browser.call.side_effect = BrowserError(status)
                    result = await Sources(mcp_call=provider, browser1111=browser).search(SearchInput(
                        criteria={'keyword': '霽禾流光'}, sources=[source], include_companies=['霽禾流光']))
                    self.assertEqual(result['source_results'][0]['status'], status)
                    self.assertFalse(result['items'])
                    self.assertEqual(result['filter_warnings'], [])
                    self.assertEqual(result['can_continue'], status != 'empty')

    async def test_include_1111_cap_preserves_overflow_for_next_display(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        browser = AsyncMock()
        browser.call.side_effect = lambda args: page([
            dict(row(str(100000 + args['page'] * 100 + i)),
                 company='霽禾流光' if args['page'] == 2 else '別家僱主') for i in range(3)],
            number=args['page'], size=3, total=9)
        service = Sources(browser1111=browser)
        arguments = SearchInput(criteria={'keyword': '霽禾流光'}, sources=['1111'], limit=2,
                                include_companies=['霽禾流光'])
        first = await service.search(arguments)
        self.assertEqual([job.source_job_id for job in first['items']], ['100200', '100201'])
        self.assertEqual(first['excluded_count'], 3)
        self.assertTrue(first['can_continue'])
        state = json.loads(first['continuation'])['sources']['1111']['areas']['all']
        self.assertFalse(state['has_next'])
        self.assertEqual([job['source_job_id'] for job in state['remaining']], ['100202'])
        second = await service.search(arguments, continuation=first['continuation'])
        self.assertEqual([job.source_job_id for job in second['items']], ['100202'])
        self.assertFalse(second['can_continue'])
        self.assertIsNone(second['continuation'])
        self.assertEqual([call.args[0]['page'] for call in browser.call.await_args_list], [1, 2])
        self.assertEqual(sum(result['filter_warnings'].count('1111指名公司的搜尋只查前2頁，未再翻頁。')
                             for result in (first, second)), 1)

    async def test_include_filters_queued_candidates_on_continuation(self):
        async def provider(name, args):
            return dict(jobs=[dict(job_no=str(i), job_name='職缺', company=company,
                url=f'https://www.104.com.tw/job/u{i}') for i, company in enumerate(
                ['霽禾流光', '別家僱主', '霽禾流光'], 1)], total=3, has_next=False), None

        service = Sources(mcp_call=provider)
        arguments = SearchInput(criteria={'keyword': '霽禾流光'}, sources=['104'], limit=1)
        first = await service.search(arguments)
        arguments.include_companies = ['霽禾流光']
        second = await service.search(arguments, continuation=first['continuation'])
        self.assertEqual([job.source_job_id for job in second['items']], ['3'])
        self.assertEqual(second['excluded_count'], 1)
        self.assertFalse(second['can_continue'])
        self.assertEqual(second['filter_warnings'], [])

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
        self.assertTrue(state['sources']['518']['areas']['all']['next_url'])
        state['sources']['518']['areas']['all']['next_url'] = 'https://www.518.com.tw/job-index-P-2.html?ad=other'
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

    def test_detail_input_accepts_one_saved_id_or_safe_104_url(self):
        from app.sources import DetailInput
        self.assertEqual(DetailInput(job_id='saved-id').job_id, 'saved-id')
        for url in ('https://www.104.com.tw/job/91mtc', 'https://www.104.com.tw/job/91mtc?jobsource=google'):
            self.assertEqual(DetailInput(url=url).url, url)
        invalid = [
            'https://evil.invalid/job/91mtc', 'https://www.104.com.tw.evil.invalid/job/91mtc',
            'https://www.518.com.tw/job/91mtc', 'http://www.104.com.tw/job/91mtc',
            'https://user@www.104.com.tw/job/91mtc', 'https://www.104.com.tw:8443/job/91mtc',
            'https://www.104.com.tw/job/91mtc#fragment', 'https://www.104.com.tw/jobs/91mtc',
            'https://www.104.com.tw/job/', 'https://www.104.com.tw/job/91mtc/extra',
            'https://www.104.com.tw/job/../admin', 'https://www.104.com.tw/job/%2e%2e',
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError): DetailInput(url=url)
        for values in ({}, {'job_id': 'saved', 'url': 'https://www.104.com.tw/job/91mtc'}):
            with self.assertRaises(ValueError): DetailInput(**values)

    async def test_detail_rechecks_url_before_source_request(self):
        from types import SimpleNamespace
        async def blocked(*args): self.fail('Invalid URL must not reach the provider')
        for url in ('https://evil.invalid/job/91mtc', 'https://www.104.com.tw/job/91mtc/extra'):
            with self.assertRaises(SourceError):
                await Sources(mcp_call=blocked).detail(SimpleNamespace(source='104', source_job_id=None, url=url))


    async def test_remote_only_uses_104_filter_and_keeps_only_numeric_remote_modes(self):
        calls = []
        values = [0, 1, 2, 9, "1"]
        async def provider(name, args):
            calls.append((name, args))
            return {"jobs": [
                {"job_no": f"remote{i}", "job_name": "實習", "company": "公司",
                 "url": f"https://www.104.com.tw/job/remote{i}", "remote": value}
                for i, value in enumerate(values)
            ], "total": len(values), "has_next": False}, None
        arguments = SearchInput(criteria={"keyword": "實習", "remote_only": True}, sources=["104"])
        result = await Sources(mcp_call=provider, interval=0).search(arguments)
        self.assertEqual(calls[0][1]["remote"], True)
        self.assertEqual([job.remote_type for job in result["items"]], ["fully_remote", "partially_remote"])
        diagnostic = result["source_results"][0]
        self.assertEqual(diagnostic["remote_excluded_count"], 3)
        self.assertEqual(diagnostic["excluded_count"], 3)
        self.assertEqual(diagnostic["parser_warning_count"], 0)
        self.assertEqual(diagnostic["parser_warnings"], [])
        self.assertEqual(diagnostic["status"], "success")
        self.assertTrue(diagnostic["baseline_eligible"])
        self.assertIn("依遠端條件排除 3 筆非遠端職缺。", result["filter_warnings"])

    async def test_remote_exclusion_stays_reliable_and_visible(self):
        async def provider(name, args):
            return {"jobs": [
                {"job_no": "remote", "job_name": "實習", "company": "公司",
                 "url": "https://www.104.com.tw/job/remote", "remote": 1},
                {"job_no": "local", "job_name": "實習", "company": "公司",
                 "url": "https://www.104.com.tw/job/local", "remote": 0},
            ], "total": 2, "has_next": False}, None
        result = await Sources(mcp_call=provider, interval=0).search(
            SearchInput(criteria={"keyword": "實習", "remote_only": True}, sources=["104"]))
        diagnostic = result["source_results"][0]
        self.assertEqual([job.source_job_id for job in result["items"]], ["remote"])
        self.assertEqual(diagnostic["remote_excluded_count"], 1)
        self.assertEqual(diagnostic["status"], "success")
        self.assertTrue(diagnostic["baseline_eligible"])
        self.assertEqual(diagnostic["parser_warnings"], [])
        self.assertIn("依遠端條件排除 1 筆非遠端職缺。", result["filter_warnings"])

    async def test_remote_exclusion_does_not_override_real_parser_warnings(self):
        async def provider(name, args):
            return {"jobs": [
                {"job_no": "local", "job_name": "實習", "company": "公司",
                 "url": "https://www.104.com.tw/job/local", "remote": 0},
                {"job_no": "remote", "job_name": "實習", "company": "公司",
                 "url": "https://www.104.com.tw/job/remote", "remote": 1},
                None,
            ], "total": 3, "has_next": False}, None
        result = await Sources(mcp_call=provider, interval=0).search(
            SearchInput(criteria={"keyword": "實習", "remote_only": True}, sources=["104"]))
        diagnostic = result["source_results"][0]
        self.assertEqual(diagnostic["remote_excluded_count"], 1)
        self.assertEqual(diagnostic["status"], "partial")
        self.assertFalse(diagnostic["baseline_eligible"])
        self.assertEqual(diagnostic["parser_warning_count"], 1)
        self.assertTrue(any("格式不完整" in warning for warning in diagnostic["parser_warnings"]))
        self.assertIn("依遠端條件排除 1 筆非遠端職缺。", result["filter_warnings"])

    async def test_remote_excluded_pages_keep_searching_until_qualified_page(self):
        calls = []
        async def provider(name, args):
            page = args["page"]
            calls.append(page)
            rows = {
                1: [("local-1", 0)],
                2: [("local-2", 0)],
                3: [("remote3", 1)],
            }[page]
            return {"jobs": [{"job_no": job_no, "job_name": "實習", "company": "公司",
                              "url": f"https://www.104.com.tw/job/{job_no}", "remote": remote}
                             for job_no, remote in rows],
                    "total": 3, "has_next": page < 3}, None
        result = await Sources(mcp_call=provider, interval=0).search(
            SearchInput(criteria={"keyword": "實習", "remote_only": True}, sources=["104"], limit=2))
        diagnostic = result["source_results"][0]
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual([job.source_job_id for job in result["items"]], ["remote3"])
        self.assertEqual(diagnostic["remote_excluded_count"], 2)
        self.assertEqual(diagnostic["status"], "success")
        self.assertTrue(diagnostic["baseline_eligible"])
        self.assertFalse(result["can_continue"])

    async def test_identical_raw_remote_excluded_pages_still_stop_as_duplicate(self):
        calls = []
        async def provider(name, args):
            page = args["page"]
            calls.append(page)
            return {"jobs": [{"job_no": "same-local", "job_name": "實習", "company": "公司",
                              "url": "https://www.104.com.tw/job/same-local", "remote": 0}],
                    "total": 3, "has_next": page < 3}, None
        result = await Sources(mcp_call=provider, interval=0).search(
            SearchInput(criteria={"keyword": "實習", "remote_only": True}, sources=["104"], limit=2))
        diagnostic = result["source_results"][0]
        self.assertEqual(calls, [1, 2])
        self.assertEqual(result["items"], [])
        self.assertFalse(result["can_continue"])
        self.assertEqual(diagnostic["status"], "partial")
        self.assertFalse(diagnostic["baseline_eligible"])
        self.assertTrue(any("重複的頁面內容" in warning for warning in diagnostic["parser_warnings"]))

    async def test_false_and_missing_remote_only_keep_the_existing_104_arguments(self):
        calls = []
        async def provider(name, args):
            calls.append(args)
            return {"jobs": [{"job_no": str(len(calls)), "job_name": "採購", "company": "公司",
                              "url": f"https://www.104.com.tw/job/c{len(calls)}"}],
                    "total": 1, "has_next": False}, None
        service = Sources(mcp_call=provider, interval=0)
        await service.search(SearchInput(criteria={"keyword": "採購"}, sources=["104"]))
        await service.search(SearchInput(criteria={"keyword": "採購", "remote_only": False}, sources=["104"]))
        expected = {"keyword": "採購", "area": [], "page": 1, "page_size": 20, "salary_min": None}
        self.assertEqual(calls, [expected, expected])
        for value in ("true", 1, 0):
            with self.subTest(remote_only=value), self.assertRaises(ValueError):
                SearchInput(criteria={"keyword": "採購", "remote_only": value})

    async def test_remote_only_marks_518_and_1111_unsupported_without_calling_them(self):
        from app.sources import area_catalog
        areas = area_catalog()[:2]
        for requested_areas in ([], areas):
            calls = []
            async def provider(name, args):
                calls.append((name, args))
                return {"jobs": [{"job_no": "remote", "job_name": "採購", "company": "公司",
                                  "url": "https://www.104.com.tw/job/r", "remote": 1}],
                        "total": 1, "has_next": False}, None
            class Browser:
                async def call(self, *args, **kwargs):
                    raise AssertionError("remote-only must not call 1111")
            def no_transport(request):
                raise AssertionError("remote-only must not call 518")
            result = await Sources(httpx.MockTransport(no_transport), provider, interval=0, browser1111=Browser()).search(
                SearchInput(criteria={"keyword": "採購", "area_codes": [item["code"] for item in requested_areas], "remote_only": True}, sources=["104", "518", "1111"]))
            self.assertEqual([name for name, _ in calls], ["search_jobs"] * max(1, len(requested_areas)))
            self.assertEqual([call[1]["area"] for call in calls], [[item["name"]] for item in requested_areas] or [[]])
            self.assertTrue(all(call[1]["remote"] is True for call in calls))
            by_source = {item["source"]: item for item in result["source_results"]}
            self.assertEqual(by_source["104"]["status"], "success")
            for source in ("518", "1111"):
                self.assertEqual(by_source[source]["status"], "unsupported")
                self.assertIn("尚未支援遠端工作篩選，本次未查", by_source[source]["message"])
            self.assertEqual([job.remote_type for job in result["items"]], ["fully_remote"])
            self.assertEqual([row["status"] for row in result["area_results"] if row["source"] == "518"],
                             ["unsupported"] * max(1, len(requested_areas)))
            self.assertEqual([row["status"] for row in result["area_results"] if row["source"] == "1111"],
                             ["unsupported"] * max(1, len(requested_areas)))

    async def test_old_false_cursor_continues_but_remote_search_cannot_reuse_it(self):
        calls = []
        async def provider(name, args):
            calls.append(args)
            page = args["page"]
            return {"jobs": [{"job_no": str(page), "job_name": "採購", "company": "公司",
                              "url": f"https://www.104.com.tw/job/c{page}"}],
                    "total": 2, "has_next": page == 1}, None
        service = Sources(mcp_call=provider, interval=0)
        false_args = SearchInput(criteria={"keyword": "採購"}, sources=["104"], limit=1)
        first = await service.search(false_args)
        state = json.loads(first["continuation"])
        self.assertNotIn("remote_only", state["criteria"])
        state["criteria"].pop("remote_only", None)
        continued = await service.search(false_args, continuation=state)
        self.assertEqual([args["page"] for args in calls], [1, 2])
        self.assertEqual(continued["items"][0].source_job_id, "2")
        new_cursor = await service.search(false_args)
        calls_before = len(calls)
        remote_args = SearchInput(criteria={"keyword": "採購", "remote_only": True}, sources=["104"], limit=1)
        rejected = await service.search(remote_args, continuation=new_cursor["continuation"])
        self.assertEqual(len(calls), calls_before)
        self.assertEqual(rejected["source_results"][0]["status"], "unsupported")
        self.assertIn("續查條件已變更", rejected["source_results"][0]["message"])
    async def test_each_104_area_has_its_own_declared_empty_failure_and_success(self):
        from app.sources import area_catalog
        areas = area_catalog()[:3]
        calls = []
        async def provider(name, args):
            calls.append(args)
            area = args['area']
            self.assertEqual(len(area), 1)
            if area == [areas[0]['name']]:
                return {'jobs': [], 'total': 0, 'has_next': False}, None
            if area == [areas[1]['name']]:
                raise SourceError('failed', '合成請求失敗')
            return {'jobs': [{'job_no': 'last1', 'job_name': '採購', 'company': '合成公司',
                'url': 'https://www.104.com.tw/job/last1'}], 'total': 1, 'has_next': False}, None
        result = await Sources(mcp_call=provider, interval=0).search(SearchInput(
            criteria={'keyword': '採購', 'area_codes': [item['code'] for item in areas]}, sources=['104']))
        rows = result['area_results']
        self.assertEqual([args['area'] for args in calls], [[item['name']] for item in areas])
        self.assertEqual([row['status'] for row in rows], ['empty', 'failed', 'success'])
        self.assertEqual([row['total_declared'] for row in rows], [0, None, 1])
        self.assertEqual([row['returned_count'] for row in rows], [0, 0, 1])
        self.assertEqual(result['source_results'][0]['status'], 'partial')
        self.assertIsNone(result['source_results'][0]['total_declared'], 'a failed region makes the source-wide total unknown')

    async def test_104_area_pages_and_items_are_interleaved_before_limit(self):
        from app.sources import area_catalog
        areas = area_catalog()[:3]
        calls = []
        async def provider(name, args):
            area_name = args['area'][0]
            calls.append(area_name)
            prefix = str(areas.index(next(item for item in areas if item['name'] == area_name)))
            jobs = [{'job_no': f'{prefix}{i:02}', 'job_name': '採購', 'company': f'公司{prefix}',
                     'url': f'https://www.104.com.tw/job/{prefix}{i:02}'} for i in range(20)]
            return {'jobs': jobs, 'total': 20, 'has_next': False}, None
        result = await Sources(mcp_call=provider, interval=0).search(SearchInput(
            criteria={'keyword': '採購', 'area_codes': [item['code'] for item in areas]}, sources=['104'], limit=20))
        by_prefix = {prefix: sum(job.source_job_id.startswith(prefix) for job in result['items']) for prefix in ('0','1','2')}
        self.assertEqual(calls, [item['name'] for item in areas])
        self.assertTrue(all(count >= 6 for count in by_prefix.values()), by_prefix)
        self.assertEqual([row['returned_count'] for row in result['area_results']], [7, 7, 6])
        cursor = json.loads(result['continuation'])['sources']['104']['areas']
        self.assertEqual(sum(len(cursor[item['code']]['remaining']) for item in areas), 40)
        self.assertTrue(result['can_continue'])

    async def test_http_deadline_marks_unqueried_areas_and_continues_only_them(self):
        from app.sources import area_catalog
        areas = area_catalog()[:3]
        clock = {'now': 0}
        calls = []
        async def provider(name, args):
            area = args['area'][0]
            calls.append(area)
            clock['now'] = 31
            suffix = str(areas.index(next(item for item in areas if item['name'] == area)))
            return {'jobs': [{'job_no': f'd{suffix}', 'job_name': '採購', 'company': '合成公司',
                'url': f'https://www.104.com.tw/job/d{suffix}'}], 'total': 1, 'has_next': False}, None
        source = Sources(mcp_call=provider, interval=0)
        args = SearchInput(criteria={'keyword': '採購', 'area_codes': [item['code'] for item in areas]}, sources=['104'])
        with patch('app.sources.time.monotonic', side_effect=lambda: clock['now']):
            first = await source.search(args, deadline=60)
        self.assertEqual(calls, [areas[0]['name']])
        self.assertEqual([row['status'] for row in first['area_results']], ['success', 'not_queried', 'not_queried'])
        self.assertTrue(first['can_continue'])
        resumed = await source.search(args, continuation=first['continuation'])
        self.assertEqual(calls, [item['name'] for item in areas])
        self.assertEqual([row['status'] for row in resumed['area_results']], ['success', 'success', 'success'])
        self.assertEqual([row['returned_count'] for row in resumed['area_results']], [1, 1, 1])

    async def test_518_unmapped_area_is_unsupported_without_skipping_mapped_area(self):
        from app.sources import area_catalog
        areas = area_catalog()[:2]
        calls = []
        def transport(request):
            calls.append(str(request.url))
            return httpx.Response(200, text='<div id="listContent">查無符合條件的職缺</div>')
        source = Sources(httpx.MockTransport(transport), interval=0)
        source.regions518 = {areas[1]['name']: '3001008'}
        result = await source.search(SearchInput(
            criteria={'keyword': '採購', 'area_codes': [item['code'] for item in areas]}, sources=['518']))
        self.assertEqual(len(calls), 1)
        self.assertIn('aa=3001008', calls[0])
        self.assertEqual([row['status'] for row in result['area_results']], ['unsupported', 'empty'])
        self.assertEqual(result['source_results'][0]['status'], 'partial')
        self.assertFalse(result['can_continue'])

    async def test_all_taiwan_is_one_area_and_keeps_the_104_parameter_shape(self):
        calls = []
        async def provider(name, args):
            calls.append(args)
            return {'jobs': [{'job_no': 'all1', 'job_name': '採購', 'company': '合成公司',
                'url': 'https://www.104.com.tw/job/all1'}], 'total': 1, 'has_next': False}, None
        result = await Sources(mcp_call=provider, interval=0).search(SearchInput(
            criteria={'keyword': '採購'}, sources=['104']))
        self.assertEqual(calls[0]['area'], [])
        self.assertEqual(result['area_results'], [dict(area_code=None, area_name='全台', source='104', status='success',
            returned_count=1, total_declared=1, message=None, completed=True)])

    async def test_v1_single_area_cursor_maps_and_multi_area_cursor_is_rejected(self):
        from app.sources import area_catalog
        areas = area_catalog()[:2]
        calls = []
        async def provider(name, args):
            calls.append((args['area'], args['page']))
            page_no = args['page']
            ident = f'old{page_no}'
            return {'jobs': [{'job_no': ident, 'job_name': '採購', 'company': '合成公司',
                'url': f'https://www.104.com.tw/job/{ident}'}], 'total': 3, 'has_next': page_no < 3}, None
        source = Sources(mcp_call=provider, interval=0)
        one = SearchInput(criteria={'keyword': '採購', 'area_codes': [areas[0]['code']]}, sources=['104'], limit=1)
        first = await source.search(one)
        cursor = json.loads(first['continuation'])
        cursor['version'] = 1
        source_state = cursor['sources']['104']
        page_state = source_state['areas'][areas[0]['code']]
        cursor['sources']['104'] = dict(page_size=source_state['page_size'], next_page=page_state['next_page'],
            next_url=page_state['next_url'], remaining=page_state['remaining'], seen_ids=source_state['seen_ids'],
            pages=page_state['pages'], visited=page_state['visited'], page_signatures=page_state['page_signatures'],
            has_next=page_state['has_next'], total_declared=page_state['total_declared'],
            parser_warnings=source_state['parser_warnings'])
        resumed = await source.search(one, continuation=cursor)
        self.assertEqual(calls[-1], ([areas[0]['name']], 2))
        self.assertEqual(resumed['items'][0].source_job_id, 'old2')
        self.assertEqual(json.loads(resumed['continuation'])['version'], 2)

        many = SearchInput(criteria={'keyword': '採購', 'area_codes': [item['code'] for item in areas]}, sources=['104'])
        old_multi = {'version': 1, 'criteria': {'keyword': '採購', 'area_codes': [item['code'] for item in areas],
            'min_monthly_salary': None}, 'sources': {'104': {'page_size': 20, 'seen_ids': [], 'parser_warnings': []}}}
        count = len(calls)
        rejected = await source.search(many, continuation=old_multi)
        self.assertEqual(len(calls), count)
        self.assertIn('續查位置版本不支援，請從原搜尋重新開始。', rejected['source_results'][0]['message'])


class MultiAreaRowChecks(unittest.IsolatedAsyncioTestCase):
    def fixture(self, count, limit=20, *, daily=False, size=20, blocked=False, pageable=False):
        from app.sources import DailySearchInput
        # Synthetic catalog tests cardinality independently of real city groupings.
        areas = [dict(code=str(910000 + index), name=f'合成區{index}') for index in range(count)]
        calls = []

        async def provider(name, args):
            calls.append(dict(args))
            index = [area['name'] for area in areas].index(args['area'][0]) if args['area'] else 0
            if blocked and index == count - 1:
                raise SourceError('blocked', '合成來源受阻')
            start = (args['page'] - 1) * args['page_size']
            jobs = [dict(job_no=f'{index:02}{number:04}', job_name='研發替代役軟體工程師',
                         company=f'合成科技股份有限公司{index}', location='合成市研發園區',
                         salary='月薪 40,000～60,000 元',
                         url=f'https://www.104.com.tw/job/a{index:02}{number:04}') for number in range(start, start + min(size, args['page_size']))]
            return dict(jobs=jobs, total=1000 if pageable else len(jobs), has_next=pageable), None

        arguments = (DailySearchInput if daily else SearchInput)(
            criteria=dict(keyword='研發替代役', area_codes=[area['code'] for area in areas]),
            sources=['104'], limit=limit)
        return areas, arguments, Sources(mcp_call=provider, interval=0), calls

    async def test_effective_row_caps_and_balanced_areas(self):
        for count, limit, daily, expected in ((0, 20, False, 20), (1, 20, False, 20),
                (2, 20, False, 20), (7, 20, False, 35), (10, 20, False, 50),
                (12, 20, False, 50), (3, 10, False, 15), (7, 100, True, 100),
                (0, 3, False, 3), (1, 3, False, 3), (4, 2, False, 8), (5, 3, False, 15)):
            with self.subTest(count=count, limit=limit, daily=daily):
                areas, arguments, service, calls = self.fixture(count, limit, daily=daily, pageable=True)
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await service.search(arguments)
                self.assertEqual(len(result['items']), expected)
                self.assertEqual(result['source_results'][0]['returned_count'], expected)
                counts = [area['returned_count'] for area in result['area_results']]
                self.assertTrue(all(value > 0 for value in counts))
                self.assertLessEqual(max(counts) - min(counts), 1)
                self.assertEqual(len(calls), max(1, count))
                self.assertTrue(all(call['page'] == 1 and call['page_size'] == min(20, limit) for call in calls))

    async def test_first_pages_return_more_without_extra_requests(self):
        for count, expected in ((7, 35), (9, 45)):
            with self.subTest(count=count):
                areas, arguments, service, calls = self.fixture(count)
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await service.search(arguments)
                self.assertEqual([(call['area'], call['page'], call['page_size']) for call in calls],
                                 [([area['name']], 1, 20) for area in areas])
                self.assertEqual(len(result['items']), expected)
                cursor = json.loads(result['continuation'])['sources']['104']
                self.assertEqual(sum(len(area['remaining']) for area in cursor['areas'].values()), count * 20 - expected)

    async def test_continuations_keep_page_size_and_effective_cap(self):
        for count, limit, expected, turns in ((7, 20, 35, 4), (3, 10, 15, 2)):
            with self.subTest(count=count, limit=limit):
                areas, arguments, service, calls = self.fixture(count, limit)
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await service.search(arguments)
                    identifiers = set()
                    for turn in range(turns):
                        self.assertEqual(len(result['items']), expected)
                        new_ids = {job.source_job_id for job in result['items']}
                        self.assertFalse(identifiers & new_ids)
                        identifiers.update(new_ids)
                        self.assertEqual(len(calls), count, 'queued rows must not fetch more pages')
                        if turn == turns - 1:
                            break
                        cursor = json.loads(result['continuation'])
                        self.assertEqual(cursor['sources']['104']['page_size'], min(20, limit))
                        rejected = await service.search(arguments.model_copy(update={'limit': limit - 1}), continuation=cursor)
                        self.assertEqual(rejected['source_results'][0]['status'], 'unsupported')
                        result = await service.search(arguments, continuation=cursor)
                self.assertFalse(result['can_continue'])
                self.assertTrue(all(area['completed'] for area in result['area_results']))
                self.assertEqual(len(identifiers), count * min(20, limit))

    async def test_quota_flags_use_effective_cap_and_preserve_exhaustion(self):
        for count, limit, size, blocked, expected, limited in (
                (7, 20, 20, False, 35, True), (3, 10, 20, False, 15, True),
                (7, 20, 5, False, 35, False), (12, 20, 4, False, 48, False),
                (7, 20, 4, True, 24, False), (3, 10, 6, True, 12, False)):
            with self.subTest(count=count, limit=limit, size=size, blocked=blocked):
                areas, arguments, service, calls = self.fixture(count, limit, size=size, blocked=blocked)
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await service.search(arguments)
                self.assertEqual(len(result['items']), expected)
                diagnostic = result['source_results'][0]
                self.assertEqual(diagnostic['quota_limited'], limited)
                self.assertEqual(diagnostic['truncated'], limited)
                self.assertEqual(result['can_continue'], limited or blocked)
                self.assertEqual(diagnostic['status'], 'partial' if limited or blocked else 'success')

    async def test_518_and_1111_use_the_same_effective_cap(self):
        from unittest.mock import AsyncMock
        from urllib.parse import parse_qs
        from test_source1111 import page, row
        for source in ('518', '1111'):
            for count, limit, expected in ((7, 20, 35), (3, 10, 15)):
                with self.subTest(source=source, count=count, limit=limit):
                    areas, arguments, _, _ = self.fixture(count, limit)
                    arguments.sources = [source]
                    names = [area['name'] for area in areas]
                    mapping = {name: str(3001000 + i) for i, name in enumerate(names)}
                    requests = []
                    def transport(request):
                        index = list(mapping.values()).index(parse_qs(request.url.query.decode())['aa'][0])
                        requests.append(index)
                        cards = ''.join(f'<div class="job__card" data-id="A{index}b{i}" data-url="/job-A{index}b{i}.html">'
                            f'<a class="job__title" href="/job-A{index}b{i}.html">研發工程師</a>'
                            '<span class="job__comp__name">合成公司</span></div>' for i in range(20))
                        return httpx.Response(200, text=f'<div id="listContent">{cards}</div>')
                    browser = AsyncMock()
                    browser.call.return_value = dict(status='ok', areas=[dict(
                        page([row(str(100000 + index * 100 + i)) for i in range(20)], size=20, total=20),
                        area_name=name) for index, name in enumerate(names)])
                    service = Sources(httpx.MockTransport(transport), browser1111=browser, interval=0)
                    service.regions518 = mapping
                    with patch('app.sources.area_catalog', return_value=areas):
                        result = await service.search(arguments)
                        self.assertEqual(len(result['items']), expected)
                        self.assertTrue(all(area['returned_count'] > 0 for area in result['area_results']))
                        self.assertEqual(result['source_results'][0]['returned_count'], expected)
                        self.assertTrue(result['source_results'][0]['quota_limited'])
                        cursor = json.loads(result['continuation'])
                        self.assertEqual(cursor['sources'][source]['page_size'], min(20, limit))
                        resumed = await service.search(arguments, continuation=cursor)
                    self.assertEqual(len(resumed['items']), expected)
                    self.assertEqual(len(requests), count if source == '518' else 0)
                    self.assertEqual(browser.call.await_count, 1 if source == '1111' else 0)

    async def test_small_limits_cover_each_first_page_without_extra_requests(self):
        for count, limit, expected in ((4, 2, 8), (5, 3, 15), (3, 1, 3)):
            with self.subTest(count=count, limit=limit):
                areas, arguments, service, calls = self.fixture(count, limit, pageable=True)
                with patch('app.sources.area_catalog', return_value=areas):
                    # Before automatic row expansion, the full first round was still fetched.
                    with patch('app.sources.AREA_ROW_FLOOR', 0):
                        original = await service.search(arguments)
                    original_calls = list(calls)
                    calls.clear()
                    result = await service.search(arguments)
                self.assertEqual(len(original['items']), limit)
                self.assertEqual(len(result['items']), expected)
                self.assertEqual(calls, original_calls)
                self.assertEqual([(call['area'], call['page'], call['page_size']) for call in calls],
                                 [([area['name']], 1, limit) for area in areas])
                self.assertTrue(result['can_continue'])

    async def test_http_second_round_uses_effective_cap_after_exclusions(self):
        from urllib.parse import parse_qs, urlencode
        for source in ('104', '518'):
            for count, retained, expected in ((3, 4, 15), (4, 3, 20)):
                with self.subTest(source=source, count=count):
                    areas, arguments, _, _ = self.fixture(count, 10)
                    arguments.sources = [source]
                    names = [area['name'] for area in areas]
                    mapping = {name: str(3001000 + i) for i, name in enumerate(names)}
                    calls = []

                    def candidates(index, page):
                        return [dict(job_no=f'{index}{page}{i:03}', job_name='研發工程師',
                                     company='合成公司' if page > 1 or i < retained else '排除公司',
                                     url=f'https://www.104.com.tw/job/a{index}{page}{i:03}')
                                for i in range(10)]

                    async def provider(name, args):
                        index = names.index(args['area'][0])
                        page = args['page']
                        calls.append((index, page))
                        return dict(jobs=candidates(index, page), total=100, has_next=True), None

                    def transport(request):
                        index = list(mapping.values()).index(parse_qs(request.url.query.decode())['aa'][0])
                        page = 2 if '-P-2' in request.url.path else 1
                        calls.append((index, page))
                        cards = ''.join(
                            f'<div class="job__card" data-id="A{job["job_no"]}" data-url="/job-A{job["job_no"]}.html">'
                            f'<a class="job__title" href="/job-A{job["job_no"]}.html">研發工程師</a>'
                            f'<span class="job__comp__name">{job["company"]}</span></div>'
                            for job in candidates(index, page))
                        query = urlencode(dict(ad=arguments.criteria.keyword, aa=list(mapping.values())[index]))
                        return httpx.Response(200, text=f'<link rel="next" href="/job-index-P-{page+1}.html?{query}">'
                                              f'<div id="listContent">{cards}</div>')

                    service = Sources(httpx.MockTransport(transport), mcp_call=provider, interval=0)
                    service.regions518 = mapping
                    self.assertGreaterEqual(count * retained, arguments.limit)
                    self.assertLess(count * retained, expected)
                    with patch('app.sources.area_catalog', return_value=areas):
                        result = await service.search(arguments, exclude_companies=['排除公司'])
                    self.assertEqual(len(result['items']), expected)
                    self.assertEqual(calls, [(i, 1) for i in range(count)] + [(i, 2) for i in range(5-count)])
                    self.assertEqual(result['source_results'][0]['pages_fetched'], 5)
                    self.assertEqual(result['excluded_count'], count * (10-retained))
                    self.assertTrue(result['can_continue'])

    async def test_1111_second_round_uses_effective_cap_after_exclusions(self):
        from unittest.mock import AsyncMock
        from test_source1111 import page, row
        for count, limit, retained, expected in ((7, 20, 3, 35), (3, 10, 4, 15)):
            with self.subTest(count=count, limit=limit):
                areas, arguments, _, _ = self.fixture(count, limit)
                arguments.sources = ['1111']
                names = [area['name'] for area in areas]
                calls = []

                async def worker(args):
                    number = args['page']
                    calls.append((list(args['area_names']), number))
                    results = []
                    for name in args['area_names']:
                        index = names.index(name)
                        jobs = [dict(row(str(100000 + index*1000 + number*100 + i)),
                                     company='合成公司' if number > 1 or i < retained else '排除公司')
                                for i in range(20)]
                        results.append(dict(page(jobs, number=number, size=20, total=100), area_name=name))
                    return dict(status='ok', areas=results)

                browser = AsyncMock()
                browser.call.side_effect = worker
                self.assertGreaterEqual(count * retained, limit)
                self.assertLess(count * retained, expected)
                with patch('app.sources.area_catalog', return_value=areas):
                    result = await Sources(browser1111=browser, interval=0).search(
                        arguments, exclude_companies=['排除公司'])
                self.assertEqual(len(result['items']), expected)
                self.assertEqual(calls, [(names, 1), (names, 2)])
                self.assertEqual(browser.call.await_count, 2)
                self.assertEqual(result['source_results'][0]['pages_fetched'], 2)
                self.assertEqual(result['excluded_count'], count * (20-retained))
                self.assertTrue(result['can_continue'])


if __name__ == '__main__': unittest.main()
