import assert from 'node:assert/strict';
import './source.budget.test.mjs';
import { after, test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { chromium } from 'playwright';
import { searchUrl, validateSearch } from './probe.mjs';
import { runSource, resolveArea, validateInput, SINGLE_AREA_MS, MAX_BUDGET_MS } from './source.mjs';
let browser;
async function prepareBrowser() {
  if (browser) return;
  browser = await chromium.launch();
  // Initialize Chinese text/layout before the bounded checks; browser.close cleans up this page.
  const readyPage = await browser.newPage();
  await readyPage.goto('about:blank', { waitUntil: 'load', timeout: 30000 });
  await readyPage.setContent('<h1>採購：合成測試職缺</h1>', { waitUntil: 'load', timeout: 30000 });
  await readyPage.waitForFunction(() => {
    const heading = document.querySelector('h1');
    return document.readyState === 'complete' && document.fonts.status === 'loaded'
      && heading.innerText === '採購：合成測試職缺'
      && heading.checkVisibility({ visibilityProperty: true, opacityProperty: true })
      && heading.getBoundingClientRect().width > 0;
  }, null, { timeout: 30000 });
}
after(async () => { await browser?.close(); });
const catalogue = [
  { code: 100100, level: 2, name: '台北市' }, { code: 100101, level: 3, parentCode: 100100, name: '中正區' },
  { code: 101600, level: 2, name: '台南市' }, { code: 101601, level: 3, parentCode: 101600, name: '中正區' },
  { code: 654321, level: 2, name: '測試縣' },
];
function nuxt(page, ids, total) {
  const values = [{ apiJob: 1 }, { result: 2 }, { pagination: 3, hits: 8 },
    { page: 4, limit: 5, totalCount: 6, totalPage: 7 }, page, 2, total, Math.ceil(total / 2), []];
  for (const id of ids) { const ref = values.length; values[8].push(ref); values.push({ jobId: ref + 1 }, id); }
  return JSON.stringify(values);
}
async function fixture(input, options = {}) {
  await prepareBrowser();
  const context = await browser.newContext();
  const requests = [];
  let closed = false;
  let catalogueRequests = 0;
  let pageCount = 0;
  const wrapper = { pages: () => context.pages(), newPage: () => { pageCount++; return context.newPage(); },
    request: { get: async url => { catalogueRequests++; assert.equal(url, 'https://www.1111.com.tw/api/v1/codeCategories/city'); return { ok: () => true, json: async () => options.catalogue || catalogue }; } },
    close: async () => { closed = true; await context.close(); } };
  await context.route('**/*', route => {
    const url = new URL(route.request().url()); requests.push(url.href);
    const number = Number(url.searchParams.get('page'));
    const ids = options.empty ? [] : number === 4 ? ['701'] : ['501', '502'];
    const keyword = url.searchParams.get('ks');
    const search = `<title>搜尋職缺</title><h1>${keyword}</h1><input placeholder="工作職稱、公司名、技能專長..." value="${keyword}">
      ${(options.partial ? ids.slice(0, 1) : ids).map(id => `<div class="job-card"><div><div><a href="/job/${id}?mid=searchlist"><h2>測試職缺 ${id}</h2></a><a href="/corp/123"><h2>測試公司</h2></a></div></div></div>`).join('')}
      <script id="__NUXT_DATA__" type="application/json">${nuxt(number, ids, options.empty ? 0 : 7)}</script>`;
    const detail = `<h1>${options.wrongTitle ? '另一職缺' : '測試職缺 501'}</h1><a href="/corp/123">測試公司</a><h2>工作內容</h2><h3>職缺描述</h3><p>合成職缺說明，負責資料整理。</p><h3>工作待遇</h3><p>測試薪資</p>`;
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: options.blocked || options.blockedArea === url.searchParams.get('c0') ? '<title>Loading...</title><body></body>' : url.pathname === '/search/job' ? search : detail });
  });
  // DOM correctness must not depend on sub-second CI browser startup.
  const result = await runSource(wrapper, input, 5000);
  assert.equal(closed, true);
  return { result, requests, catalogueRequests, pageCount };
}
const search = { action: 'search', keyword: '測試工作', area_names: [], page: 3 };
const detail = { action: 'detail', job: { id: '501', url: 'https://www.1111.com.tw/job/501', title: '測試職缺 501', company: '測試公司', companyUrl: 'https://www.1111.com.tw/corp/123/' } };
test('only chosen complete page is fetched, including short last page', async () => {
  const { result, requests } = await fixture(search);
  assert.equal(result.status, 'ok'); assert.deepEqual(result.jobs.map(j => j.id), ['501', '502']);
  assert.deepEqual([result.page, result.page_size, result.total, result.has_next], [3, 2, 7, true]);
  assert.equal(requests.length, 1); assert.equal(new URL(requests[0]).searchParams.get('page'), '3');
  const { result: last } = await fixture({ ...search, page: 4 });
  assert.equal(last.status, 'ok'); assert.deepEqual(last.jobs.map(j => j.id), ['701']); assert.equal(last.has_next, false);
});
test('stable partial page fails rather than returning its first job', async () => {
  const { result } = await fixture(search, { partial: true }); assert.equal(result.status, 'failed'); assert.equal('jobs' in result, false);
});
test('verified total zero succeeds, Loading does not become empty success', async () => {
  const { result: empty } = await fixture({ ...search, page: 1 }, { empty: true });
  assert.equal(empty.status, 'ok'); assert.deepEqual(empty.jobs, []); assert.equal(empty.total, 0); assert.equal(empty.has_next, false);
  const { result: blocked } = await fixture(search, { blocked: true }); assert.equal(blocked.status, 'blocked'); assert.equal('jobs' in blocked, false);
});
test('detail identity and company link with optional slash are verified', async () => {
  const { result, requests } = await fixture(detail);
  assert.equal(result.status, 'ok'); assert.equal(result.detail.id, '501'); assert.equal(result.detail.company, '測試公司');
  assert.deepEqual(result.detail.companyUrls, ['https://www.1111.com.tw/corp/123']); assert.match(result.detail.description, /合成職缺/); assert.equal(requests.length, 1);
  assert.equal((await fixture(detail, { wrongTitle: true })).result.status, 'failed');
});
test('invalid detail identity never navigates or exposes diagnostics', async () => {
  for (const job of [{ ...detail.job, url: 'https://other.invalid/job/501' }, { ...detail.job, id: '502' },
    { ...detail.job, companyUrl: undefined }, { ...detail.job, url: 'https://www.1111.com.tw/job/501?token=private' }]) {
    const { result, requests } = await fixture({ action: 'detail', job }); assert.equal(result.status, 'failed'); assert.deepEqual(requests, []); assert.doesNotMatch(JSON.stringify(result), /private|other\.invalid|invalid detail/);
  }
});
test('official names, 台/臺 and unseen codes resolve without guessing', () => {
  assert.equal(resolveArea(['臺北市'], catalogue), '100100'); assert.equal(resolveArea(['台南市中正區'], catalogue), '101601');
  assert.equal(resolveArea(['測試縣'], catalogue), '654321'); assert.equal(resolveArea(['中正區'], catalogue), null);
  assert.equal(resolveArea(['不存在的城市'], catalogue), null); assert.equal(resolveArea(['台北市', '台南市'], catalogue), null);
  assert.equal(resolveArea(['台北市'], [...catalogue, { code: 666666, level: 2, name: '臺北市' }]), null);
});
test('unsupported area and page above 5 never broaden the query', async () => {
  const { result, requests } = await fixture({ ...search, area_names: ['臺北市'] });
  assert.equal(result.status, 'ok'); assert.equal(new URL(requests[0]).searchParams.get('c0'), '100100');
  for (const input of [{ ...search, area_names: ['不存在的城市'] }, { ...search, page: 6 }]) {
    const { result, requests } = await fixture(input); assert.equal(result.status, 'unsupported'); assert.deepEqual(requests, []);
  }
});

test('multiple regions navigate sequentially in one page and fetch one official catalogue', async () => {
  const { result, requests, catalogueRequests, pageCount } = await fixture({ ...search, area_names: ['臺北市', '台南市', '測試縣'] });
  assert.equal(result.status, 'ok');
  assert.equal(catalogueRequests, 1); assert.equal(pageCount, 1);
  assert.deepEqual(requests.map(url => new URL(url).searchParams.get('c0')), ['100100', '101600', '654321']);
  assert.deepEqual(result.areas.map(area => area.area_name), ['臺北市', '台南市', '測試縣']);
  for (const area of result.areas) {
    assert.equal(area.status, 'ok'); assert.deepEqual(area.jobs.map(job => job.id), ['501', '502']);
    assert.deepEqual([area.page, area.page_size, area.total, area.has_next, area.warnings], [3, 2, 7, true, []]);
  }
  assert.doesNotMatch(JSON.stringify(result), /只支援單一地區/);
});

test('blocked region stops same-context navigation and explicitly marks remaining regions unqueried', async () => {
  const { result, requests, catalogueRequests, pageCount } = await fixture({ ...search, area_names: ['台北市', '台南市', '測試縣'] }, { blockedArea: '101600' });
  assert.equal(catalogueRequests, 1); assert.equal(pageCount, 1);
  assert.deepEqual(result.areas.map(area => area.status), ['ok', 'blocked', 'blocked']);
  assert.deepEqual(requests.map(url => new URL(url).searchParams.get('c0')), ['100100', '101600']);
  assert.match(result.areas[2].message, /未查/);
  for (const area of result.areas.slice(1)) assert.equal('jobs' in area, false);
});

test('unknown and ambiguous region failures do not broaden or erase the valid region', async () => {
  const { result, requests, catalogueRequests } = await fixture({ ...search, area_names: ['台南市', '台北市', '不存在的城市'] },
    { catalogue: [...catalogue, { code: 666666, level: 2, name: '臺北市' }] });
  assert.equal(catalogueRequests, 1);
  assert.deepEqual(result.areas.map(area => area.status), ['ok', 'unsupported', 'unsupported']);
  assert.equal(requests.length, 1); assert.equal(new URL(requests[0]).searchParams.get('c0'), '101600');
  for (const area of result.areas.slice(1)) {
    assert.match(area.message, /無法唯一對應/); assert.equal('jobs' in area, false);
  }
});
test('budget validation rejects non-positive, fractional and excessive budgets', () => {
  for (const budget_ms of [0, -1, 1.5, '45000', null, MAX_BUDGET_MS + 1]) {
    assert.throws(() => validateInput({ ...search, budget_ms }), /invalid budget/);
  }
  for (const budget_ms of [1, SINGLE_AREA_MS, MAX_BUDGET_MS]) validateInput({ ...search, budget_ms });
});

test('budget returns two complete regions and defers every later region without navigation', async () => {
  let elapsed = 0, currentUrl, closed = false;
  const requests = [], timeouts = [];
  const page = {
    setDefaultTimeout: value => timeouts.push(value), setDefaultNavigationTimeout: value => timeouts.push(value),
    bringToFront: async () => {}, on: () => {}, off: () => {},
    goto: async (url, options) => { currentUrl = url; requests.push(url); timeouts.push(options.timeout); elapsed += 20000; },
    evaluate: async () => {
      elapsed += 20000;
      return { url: currentUrl, title: '搜尋職缺', heading: search.keyword, keywordInput: search.keyword,
        jobs: [{ id: '501', title: '合成職缺', company: '合成公司', url: 'https://www.1111.com.tw/job/501', companyUrl: 'https://www.1111.com.tw/corp/123' }],
        initialResult: { page: 1, limit: 1, totalCount: 1, ids: ['501'] } };
    },
  };
  const context = { pages: () => [page], close: async () => { closed = true; },
    request: { get: async (url, options) => { timeouts.push(options.timeout); return { ok: () => true, json: async () => catalogue }; } } };
  const result = await runSource(context, { ...search, page: 1, budget_ms: 100000,
    area_names: ['台北市', '台南市', '測試縣', '台南市中正區'] }, 30000, () => elapsed);
  assert.equal(closed, true); assert.equal(requests.length, 2);
  assert.deepEqual(result.areas.map(area => area.status), ['ok', 'ok', 'time_limited', 'time_limited']);
  for (const area of result.areas.slice(0, 2)) {
    assert.deepEqual(area.jobs.map(job => job.id), ['501']);
    assert.deepEqual([area.page, area.page_size, area.total, area.has_next], [1, 1, 1, false]);
  }
  for (const area of result.areas.slice(2)) {
    assert.equal(area.message, '時間不足，此地區未查；可按續查補查。'); assert.equal('jobs' in area, false);
  }
  assert.ok(timeouts.every(value => value > 0 && value <= 30000));
});

test('DOM wait uses the remaining area budget after a slow navigation', async () => {
  let elapsed = 0, reads = 0, closed = false;
  const page = { setDefaultTimeout: () => {}, setDefaultNavigationTimeout: () => {},
    bringToFront: async () => {}, on: () => {}, off: () => {},
    goto: async (url, options) => { assert.equal(options.timeout, 30000); elapsed = SINGLE_AREA_MS - 1; },
    evaluate: () => { reads++; return new Promise(() => {}); } };
  const result = await runSource({ pages: () => [page], close: async () => { closed = true; } },
    { ...search, budget_ms: SINGLE_AREA_MS }, 30000, () => elapsed);
  assert.equal(result.status, 'failed'); assert.equal(closed, true); assert.equal(reads, 2);
}, { timeout: 1000 });

test('invalid stdin returns one safe JSON line before launching Chrome', () => {
  for (const raw of ['not-json', JSON.stringify({ action: 'run', code: 'private' }), 'x'.repeat(32769)]) {
    const child = spawnSync(process.execPath, ['source.mjs'], { cwd: new URL('.', import.meta.url), input: raw, encoding: 'utf8' });
    assert.equal(child.status, 0); assert.equal(child.stderr, ''); assert.equal(child.stdout.trim().split('\n').length, 1);
    assert.deepEqual(JSON.parse(child.stdout), { status: 'failed', message: '1111 查詢參數不正確。' });
  }
});

test('final stalled DOM read keeps prior validation failure without extending deadline', async () => {
  const { waitForValidDom } = await import('./probe.mjs');
  let reads = 0;
  const page = { evaluate: async () => { if (++reads === 1) return {}; return new Promise(() => {}); } };
  await assert.rejects(waitForValidDom(page, () => {}, () => { throw new Error('incomplete_page: known missing IDs'); }, 280), /incomplete_page/);
  assert.equal(reads, 2);
  await assert.rejects(waitForValidDom({ evaluate: () => new Promise(() => {}) }, () => {}, () => {}, 10), /dom_read_timeout/);
});


async function mockSearchMetadata(fields, apiHits, { preview = false } = {}) {
  const ids = fields.map(field => field.id);
  const limit = ids.length - (preview ? 1 : 0);
  const jobs = ids.slice(0, limit).reverse().map(id => ({ id, title: 'Synthetic job', company: 'Synthetic company',
    url: 'https://www.1111.com.tw/job/' + id, companyUrl: 'https://www.1111.com.tw/corp/123' }));
  const pagination = { page: 1, limit, totalCount: ids.length, totalPage: Math.ceil(ids.length / limit) };
  let currentUrl, onResponse, closed = false;
  const page = {
    setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, bringToFront: async () => {},
    on: (event, listener) => { assert.equal(event, 'response'); onResponse = listener; },
    off: (event, listener) => { assert.equal(event, 'response'); assert.equal(listener, onResponse); onResponse = undefined; },
    goto: async url => {
      currentUrl = url;
      if (apiHits) await onResponse({
        url: () => 'https://www.1111.com.tw/api/v1/search/jobs/?page=1', ok: () => true,
        json: async () => ({ result: { pagination, hits: apiHits } }),
      });
    },
    evaluate: async read => {
      assert.equal(read.name, 'readSearch');
      return { url: currentUrl, heading: search.keyword, keywordInput: search.keyword, jobs,
        initialResult: { ...pagination, ids, fields, source: 'html-ssr' } };
    },
  };
  const result = await runSource({ pages: () => [page], close: async () => { closed = true; } },
    { ...search, page: 1 }, 1000);
  assert.equal(closed, true); assert.equal(onResponse, undefined);
  return result;
}

const searchFields = [
  { id: '101', salary: '面議（經常性薪資達4萬元或以上）', location: '合成市北區' },
  { id: '103', salary: '月薪 35,000元~50,000元', location: '合成市南區' },
  { id: '105', salary: '月薪 50,000元以上', location: '合成市東區' },
  { id: '202', salary: '時薪 220元~400元', location: '合成市西區' },
  { id: '204', salary: '論件計酬 5,000元~50,000元', location: '合成市中央區' },
];

test('runSource attaches SSR fields by jobId with reordered cards', async () => {
  // Without the worker field join, salary/location are absent; positional joins assign another job's values.
  const result = await mockSearchMetadata(searchFields);
  assert.equal(result.status, 'ok');
  assert.deepEqual(result.jobs.map(({ id, salary, location }) => ({ id, salary, location })), [...searchFields].reverse());
});

test('runSource attaches API fields by jobId without borrowing SSR fields', async () => {
  // Without API extraction/join values disappear; using SSR fields returns stale values or fills intentional gaps.
  const apiHits = [
    { jobId: '105', salary: 'API monthly 50000', workCity: { name: 'API east' } },
    { jobId: '101', salary: 'API negotiable', workCity: { name: 'API north' } },
    { jobId: '204', workCity: { name: 'API centre' } },
    { jobId: '103', salary: 'API monthly 35000', workCity: { name: 'API south' } },
    { jobId: '202', salary: 'API hourly 220' },
  ];
  const result = await mockSearchMetadata(searchFields, apiHits);
  assert.equal(result.status, 'ok');
  assert.deepEqual(result.jobs.map(job => [job.id, job.salary, job.location]), [
    ['204', undefined, 'API centre'], ['202', 'API hourly 220', undefined],
    ['105', 'API monthly 50000', 'API east'], ['103', 'API monthly 35000', 'API south'],
    ['101', 'API negotiable', 'API north'],
  ]);
  assert.equal(Object.hasOwn(result.jobs[0], 'salary'), false);
  assert.equal(Object.hasOwn(result.jobs[1], 'location'), false);
});

test('runSource keeps complete pages ok after invalid optional fields are omitted', async () => {
  // Without optional API validation, malformed values leak; without the join, each valid counterpart disappears.
  // SSR fields here represent readSearch's sanitized output, whose parsing is covered in probe.test.mjs.
  const fields = [], hits = [];
  for (const invalid of [undefined, null, 42, 'x'.repeat(501), '   ']) {
    const salaryId = String(301 + fields.length);
    fields.push({ id: salaryId, location: 'Synthetic city' });
    hits.push({ jobId: salaryId, salary: invalid, workCity: { name: 'Synthetic city' } });
    const locationId = String(301 + fields.length);
    fields.push({ id: locationId, salary: 'Hourly 220' });
    hits.push({ jobId: locationId, salary: 'Hourly 220', workCity: { name: invalid } });
  }
  for (const workCity of [undefined, null]) {
    const id = String(301 + fields.length);
    fields.push({ id, salary: 'Hourly 220' });
    hits.push({ jobId: id, salary: 'Hourly 220', workCity });
  }
  for (const apiHits of [undefined, hits]) {
    const result = await mockSearchMetadata(fields, apiHits);
    assert.equal(result.status, 'ok');
    assert.deepEqual(result.jobs.map(job => {
      const { id, salary, location } = job;
      assert.equal(Object.hasOwn(job, salary === undefined ? 'salary' : 'location'), false);
      return { id, ...(salary === undefined ? {} : { salary }), ...(location === undefined ? {} : { location }) };
    }), [...fields].reverse());
  }
});

function previewState({ page = 1, limit = 3, totalCount = 8, ids = ['601', '602', '603', '604'],
  renderedIds = ids.slice(0, Math.min(limit, Math.max(0, totalCount - (page - 1) * limit))) } = {}) {
  const url = new URL(searchUrl('合成工作'));
  url.searchParams.set('page', String(page));
  return { url: url.href, heading: '合成工作', keywordInput: '合成工作',
    jobs: renderedIds.map(id => ({ id, title: '合成職缺', company: '合成公司', companyUrl: 'https://www.1111.com.tw/corp/1' })),
    initialResult: { page, limit, totalCount, ids } };
}

test('validateSearch accepts one next-page preview, full page 2 and short last page', () => {
  for (const state of [previewState(), previewState({ page: 2, ids: ['604', '605', '606'] }),
    previewState({ page: 3, ids: ['607', '608'] })]) {
    assert.doesNotThrow(() => validateSearch(state, searchUrl('合成工作'), state.initialResult.page));
    assert.doesNotThrow(() => validateSearch({ ...state, initialResult: null }, searchUrl('合成工作'),
      state.initialResult.page, state.initialResult));
  }
});

for (const [name, options] of [
  ['two extra IDs', { ids: ['601', '602', '603', '604', '605'] }],
  ['extra ID without a next page', { totalCount: 3 }],
  ['extra ID on a short last page', { page: 3, ids: ['607', '608', '609'] }],
  ['preview duplicating a current-page ID', { ids: ['601', '602', '603', '601'] }],
  ['preview replacing a current-page ID', { renderedIds: ['601', '602', '604'] }],
  ['preview rendered in addition to the full current page', { renderedIds: ['601', '602', '603', '604'] }],
]) {
  test('validateSearch rejects ' + name, () => {
    const state = previewState(options);
    assert.throws(() => validateSearch(state, searchUrl('合成工作'), state.initialResult.page), /incomplete_page/);
    assert.throws(() => validateSearch({ ...state, initialResult: null }, searchUrl('合成工作'),
      state.initialResult.page, state.initialResult), /incomplete_page/);
  });
}

test('runSource excludes next-page preview jobs and fields for both SSR and API metadata', async () => {
  const apiHits = searchFields.map(field => ({ jobId: field.id, salary: 'API ' + field.salary,
    workCity: { name: 'API ' + field.location } }));
  for (const hits of [undefined, apiHits]) {
    const result = await mockSearchMetadata(searchFields, hits, { preview: true });
    assert.equal(result.status, 'ok');
    assert.deepEqual([result.page, result.page_size, result.total, result.has_next], [1, 4, 5, true]);
    assert.equal(result.jobs.length, 4);
    assert.deepEqual(result.jobs.map(({ id, salary, location }) => ({ id, salary, location })),
      searchFields.slice(0, -1).reverse().map(field => hits
        ? { id: field.id, salary: 'API ' + field.salary, location: 'API ' + field.location } : field));
    assert.equal(result.jobs.some(job => job.id === searchFields.at(-1).id), false);
  }
});
