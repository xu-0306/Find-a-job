import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, readFile, rm, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { failureStatus, readDetail, runProbe, searchUrl, validateDetail, validateSearch } from './probe.mjs';

const driverName = process.env.PROBE_DRIVER || 'playwright';
assert.ok(['playwright', 'patchright'].includes(driverName), 'unknown PROBE_DRIVER');
const { chromium } = await import(driverName);

let browser;
before(async () => {
  browser = await chromium.launch(driverName === 'patchright' ? { channel: 'chrome' } : {});
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
}, { timeout: 30000 });
after(async () => { await browser?.close(); });

// Synthetic DOM based on observed anchors, headings and pagination; never visits the live site.
function sourceFixture(number, { repeat = false, fullPage = false } = {}) {
  const ids = number === 1 || repeat ? (fullPage ? ['101', '103', '105'] : ['101'])
    : (fullPage ? ['202', '204'] : ['202']);
  const limit = fullPage ? 3 : 1;
  const totalCount = fullPage ? 5 : 2;
  return { pagination: { page: number, limit, totalCount, totalPage: Math.ceil(totalCount / limit) }, hits: ids.map(jobId => ({ jobId })) };
}

function nuxtFixture({ pagination, hits }) {
  const values = [{ apiJob: 1 }, { result: 2 }, { pagination: 3, hits: 8 },
    { page: 4, limit: 5, totalCount: 6, totalPage: 7 },
    pagination.page, pagination.limit, pagination.totalCount, pagination.totalPage, []];
  for (const { jobId } of hits) {
    const ref = values.length;
    values[8].push(ref);
    values.push({ jobId: ref + 1 }, jobId);
  }
  return JSON.stringify(values);
}

function searchFixture(number, { repeat = false, wrongQuery = false, delayed = false, missingCompany = false, fullPage = false, neverComplete = false, responseMetadata = false } = {}) {
  const id = number === 1 || repeat ? '101' : '202';
  const source = sourceFixture(number, { repeat, fullPage });
  const extraCards = source.hits.slice(1).map(({ jobId }) => '<div class="job-card__content"><a href="/job/' + jobId + '?mid=searchlist"><h2>採購 ' + jobId + '</h2></a><a href="/corp/789" title="測試公司"><h2>測試公司</h2></a></div>').join('');
  return `<title>測試搜尋</title><h1>${wrongQuery ? '別的工作' : '採購'} 全部工作職缺</h1>
    <input placeholder="工作職稱、公司名、技能專長..." value="採購">
    <div class="job-card__content"><a href="/job/${id}?mid=searchlist"><h2>採購 ${id}</h2></a>
      ${missingCompany ? '<div>公司載入中</div>' : '<div><a href="/corp/789" title="測試公司"><h2>測試公司</h2><h3>測試產業</h3></a></div>'}</div>
    <div class="job-card__content"><a href="/job/${id}?mid=searchlist"><h2>採購 ${id}</h2></a></div>
    <a href="/job/999">首頁推薦，不是搜尋結果</a>
    <aside><a href="/job/998?mid=searchlist"><span>優先推薦</span><h2>側欄廣告</h2><p>廣告公司</p></a></aside>
    <div class="job-card__content"><a href="https://other.invalid/job/888?mid=searchlist"><h2>不同網站</h2></a></div>
    <div class="job-card__content" hidden><a href="/job/777?mid=searchlist"><h2>隱藏結果</h2></a></div>
    <button class="pagination-button" title="前往2頁" onclick="location.href='/search/job?page=2&ks=%E6%8E%A1%E8%B3%BC'">2</button>
    <script id="__NUXT_DATA__" type="application/json">${nuxtFixture(responseMetadata && number === 2 ? sourceFixture(1, { repeat, fullPage }) : source)}</script>
    <script>
      ${fullPage && !neverComplete ? 'setTimeout(() => document.body.insertAdjacentHTML("beforeend", ' + JSON.stringify(extraCards) + '), 150);' : ''}
      ${responseMetadata && number === 2 ? 'fetch("/api/v1/search/jobs/?page=2");' : ''}
    </script>
    ${delayed && number === 2 ? `<script>
      const oldCard = document.querySelector('a[href*=searchlist]');
      oldCard.href='/job/101?mid=searchlist'; oldCard.querySelector('h2').textContent='採購 101';
      document.querySelectorAll('a[href*=searchlist]')[1].remove();
      setTimeout(()=>{oldCard.href='/job/202?mid=searchlist'; oldCard.querySelector('h2').textContent='採購 202'}, 150);
    </script>` : ''}`;
}

function detailFixture(id) {
  return `<title>測試職缺</title><h1>採購 ${id}</h1><a href="/corp/789">測試公司</a>
    <h2>工作內容</h2><h3>職缺描述</h3><p>這是合成職缺，負責採購詢價、追蹤交期與核對庫存；不是真實公司或職缺。</p>
    <h3>工作待遇</h3><p>月薪 30,000 至 40,000 元</p><h2>工作地點</h2><p>測試市測試區</p>
    <h2>要求條件</h2><p>歡迎具備文書處理能力的測試應徵者。</p>`;
}

async function fixtureRun(options = {}) {
  const context = await browser.newContext();
  const existingPage = options.existingPage ? await context.newPage() : undefined;
  const outputDir = await mkdtemp(join(tmpdir(), '1111-probe-test-'));
  const requests = [];
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    requests.push(url.pathname);
    if (url.pathname === '/api/v1/search/jobs/') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ result: sourceFixture(Number(url.searchParams.get('page')), options) }) });
    }
    const html = options.blocked ? '<title>Loading...</title><body></body>'
      : options.empty ? '<title>搜尋頁</title><h1>採購</h1><p>目前沒有職缺</p>'
      : url.pathname === '/search/job' ? searchFixture(Number(url.searchParams.get('page')), options)
      : detailFixture(url.pathname.split('/').pop());
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
  });
  try {
    // Allow cold CI browsers; the dedicated stalled-read test verifies the short deadline.
    const report = await runProbe(context, { keyword: '採購', outputDir, timeoutMs: 5000, pauseMs: 0, runtime: { driver: driverName, browserVersion: browser.version() } });
    assert.deepEqual(JSON.parse(await readFile(join(outputDir, 'report.json'), 'utf8')), report);
    if (existingPage) assert.equal(existingPage.isClosed(), true, 'the existing page must be used and closed');
    return { report, requests };
  } finally {
    await context.close();
    await rm(join(outputDir, 'report.json'), { force: true });
    await rm(join(outputDir, 'failure.png'), { force: true });
    await rmdir(outputDir);
  }
}

test('query validation and URL encoding', () => {
  const url = new URL(searchUrl('C++ & 採購', '101600'));
  assert.equal(url.searchParams.get('ks'), 'C++ & 採購');
  assert.equal(url.searchParams.get('c0'), '101600');
  assert.throws(() => searchUrl(' '));
  assert.throws(() => searchUrl('採購', 'not-an-area'));
});

test('search → UI page 2 → details; excludes recommendations and duplicates', async () => {
  const { report, requests } = await fixtureRun();
  assert.equal(report.status, 'passed', JSON.stringify(report.error));
  assert.equal(report.runtime.driver, driverName);
  assert.deepEqual(report.pages.map(p => p.jobs.map(j => j.id)), [['101'], ['202']]);
  assert.equal(report.pages[1].overlap, 0);
  assert.deepEqual(report.details.map(d => d.id), ['101', '202']);
  assert.match(report.details[0].text, /月薪 30,000/);
  assert.deepEqual(requests, ['/search/job', '/search/job', '/job/101', '/job/202']);
});

test('uses and closes the initial persistent-context page', async () => {
  const { report } = await fixtureRun({ existingPage: true });
  assert.equal(report.status, 'passed', JSON.stringify(report.error));
});

test('loading page is not an empty successful search; stops before more requests', async () => {
  const { report, requests } = await fixtureRun({ blocked: true });
  assert.equal(report.status, 'blocked_or_loading');
  assert.deepEqual(report.stages, { search: 'failed', pagination: 'not_run', details: 'not_run' });
  assert.deepEqual(requests, ['/search/job']);
  assert.equal(failureStatus({ title: '採購', text: 'captcha script on a working page' }), 'failed');
});

test('empty result cannot prove pagination or detail feasibility', async () => {
  const { report } = await fixtureRun({ empty: true });
  assert.equal(report.status, 'failed');
  assert.equal(report.stages.pagination, 'not_run');
});

test('an incomplete real search card is rejected rather than skipped', async () => {
  const { report } = await fixtureRun({ missingCompany: true });
  assert.equal(report.stages.search, 'failed');
  assert.match(report.error.message, /incomplete_search_card/);
  assert.equal(report.stages.pagination, 'not_run');
});

test('wrong query content cannot pass', async () => {
  const { report } = await fixtureRun({ wrongQuery: true });
  assert.equal(report.status, 'failed');
  assert.equal(report.stages.search, 'failed');
});

test('URL page change with repeated IDs cannot pass pagination', async () => {
  const { report } = await fixtureRun({ repeat: true });
  assert.equal(report.stages.search, 'passed');
  assert.equal(report.stages.pagination, 'failed');
  assert.match(report.error.message, /pagination_unchanged/);
  assert.equal(report.stages.details, 'not_run');
});

test('waits for delayed SPA card rendering after URL changes', async () => {
  const { report } = await fixtureRun({ delayed: true });
  assert.equal(report.status, 'passed', JSON.stringify(report.error));
  assert.equal(report.pages[1].jobs[0].id, '202');
});

test('waits for every source ID, including a shorter last page and an API update over stale SSR', async () => {
  const { report, requests } = await fixtureRun({ fullPage: true, responseMetadata: true });
  assert.equal(report.status, 'passed', JSON.stringify(report.error));
  assert.deepEqual(report.pages.map(p => p.jobs.map(j => j.id)), [['101', '103', '105'], ['202', '204']]);
  assert.deepEqual(report.pages.map(p => p.completeness.expectedCount), [3, 2]);
  assert.deepEqual(report.pages.map(p => p.completeness.source), ['html-ssr', 'search-response']);
  assert.ok(requests.includes('/api/v1/search/jobs/'));
});

test('a stable partial page never passes completeness', async () => {
  const { report, requests } = await fixtureRun({ fullPage: true, neverComplete: true });
  assert.equal(report.stages.search, 'failed');
  assert.equal(report.stages.pagination, 'not_run');
  assert.match(report.error.message, /incomplete_page/);
  assert.deepEqual(requests, ['/search/job']);
});

test('equal counts cannot hide wrong IDs, duplicates, missing metadata or a stale source page', () => {
  const state = { url: searchUrl('採購'), heading: '採購', keywordInput: '採購',
    jobs: [{ id: '12', title: '採購', company: '公司', companyUrl: 'https://www.1111.com.tw/corp/1' }],
    initialResult: { page: 1, limit: 1, totalCount: 2, ids: ['12'] } };
  assert.doesNotThrow(() => validateSearch(state, searchUrl('採購'), 1));
  for (const source of [null, { ...state.initialResult, ids: ['99'] },
    { ...state.initialResult, limit: 2, ids: ['12', '12'] }, { ...state.initialResult, page: 2 },
    { ...state.initialResult, limit: 0 }]) {
    assert.throws(() => validateSearch(state, searchUrl('採購'), 1, source), /incomplete_page/);
  }
});

test('empty or hidden description does not borrow navbar or following-section text', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<nav>網站導覽</nav><h1>採購</h1><h2>工作內容</h2><p hidden>尚未載入的文字</p><p style="visibility:hidden">CSS隱藏文字</p><p style="opacity:0">透明文字</p><h2>工作待遇</h2><p>月薪</p>');
    const detail = await page.evaluate(readDetail);
    assert.equal(detail.description, '');
  } finally { await page.close(); }
});

test('empty nested job description does not borrow salary text', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<h1>採購</h1><h2>工作內容</h2><h3>職缺描述</h3><p hidden>尚未載入</p><h3>工作待遇</h3><p>月薪</p>');
    const detail = await page.evaluate(readDetail);
    assert.equal(detail.description, '');
  } finally { await page.close(); }
});

test('detail identity and missing content are rejected', () => {
  const job = { url: 'https://www.1111.com.tw/job/101', title: '採購', companyUrl: 'https://www.1111.com.tw/corp/789' };
  const detail = { url: job.url, title: job.title, companyUrls: [job.companyUrl], description: '職缺描述', text: '頁面全文' };
  assert.doesNotThrow(() => validateDetail(detail, job));
  assert.throws(() => validateDetail({ ...detail, url: 'https://other.invalid/job/101' }, job), /identity_mismatch/);
  assert.throws(() => validateDetail({ ...detail, title: '另一份工作' }, job), /identity_mismatch/);
  assert.throws(() => validateDetail({ ...detail, companyUrls: [] }, job), /identity_mismatch/);
  assert.throws(() => validateDetail({ ...detail, description: '' }, job), /incomplete_detail/);
  assert.throws(() => validateDetail({ ...detail, description: 'Loading...' }, job), /incomplete_detail/);
});

test('a stalled browser evaluation times out, records failure and still closes the page', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), '1111-stalled-read-'));
  let closed = false;
  let screenshotAttempted = false;
  const page = {
    on() {}, off() {}, setDefaultTimeout() {}, setDefaultNavigationTimeout() {},
    async bringToFront() {}, async goto() {},
    evaluate: () => new Promise(() => {}),
    async screenshot() { screenshotAttempted = true; },
    async close() { closed = true; },
  };
  try {
    const report = await runProbe({ pages: () => [], newPage: async () => page }, { keyword: '工程師', outputDir, timeoutMs: 30, pauseMs: 0 });
    assert.deepEqual(report.stages, { search: 'failed', pagination: 'not_run', details: 'not_run' });
    assert.match(report.error.message, /dom_read_timeout/);
    assert.match(report.diagnosticError, /dom_read_timeout/);
    assert.equal(screenshotAttempted, true);
    assert.equal(closed, true);
    assert.deepEqual(JSON.parse(await readFile(join(outputDir, 'report.json'), 'utf8')), report);
  } finally {
    await rm(join(outputDir, 'report.json'), { force: true });
    await rmdir(outputDir);
  }
});
