import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, readFile, rm, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { failureStatus, readDetail, runProbe, searchUrl, validateDetail } from './probe.mjs';

let browser;
before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });

// Synthetic DOM based on observed anchors, headings and pagination; never visits the live site.
function searchFixture(number, { repeat = false, wrongQuery = false, delayed = false } = {}) {
  const id = number === 1 || repeat ? '101' : '202';
  return `<title>測試搜尋</title><h1>${wrongQuery ? '別的工作' : '採購'} 全部工作職缺</h1>
    <input placeholder="工作職稱、公司名、技能專長..." value="採購">
    <div><a href="/job/${id}?mid=searchlist"><h2>採購 ${id}</h2></a>
      <div><a href="/corp/789" title="測試公司"><h2>測試公司</h2><h3>測試產業</h3></a></div></div>
    <div><a href="/job/${id}?mid=searchlist"><h2>採購 ${id}</h2></a></div>
    <a href="/job/999">首頁推薦，不是搜尋結果</a>
    <a href="https://other.invalid/job/888?mid=searchlist">不同網站</a>
    <div hidden><a href="/job/777?mid=searchlist">隱藏結果</a></div>
    <button class="pagination-button" title="前往2頁" onclick="location.href='/search/job?page=2&ks=%E6%8E%A1%E8%B3%BC'">2</button>
    ${delayed && number === 2 ? `<script>
      const oldCard = document.querySelector('a[href*=searchlist]');
      oldCard.href='/job/101?mid=searchlist'; oldCard.querySelector('h2').textContent='採購 101';
      document.querySelectorAll('a[href*=searchlist]')[1].remove();
      setTimeout(()=>{oldCard.href='/job/202?mid=searchlist'; oldCard.querySelector('h2').textContent='採購 202'}, 150);
    </script>` : ''}`;
}

function detailFixture(id) {
  return `<title>測試職缺</title><h1>採購 ${id}</h1><a href="/corp/789">測試公司</a>
    <h2>工作內容</h2><p>這是合成職缺，負責採購詢價、追蹤交期與核對庫存；不是真實公司或職缺。</p>
    <h2>工作待遇</h2><p>月薪 30,000 至 40,000 元</p><h2>工作地點</h2><p>測試市測試區</p>
    <h2>要求條件</h2><p>歡迎具備文書處理能力的測試應徵者。</p>`;
}

async function fixtureRun(options = {}) {
  const context = await browser.newContext();
  const outputDir = await mkdtemp(join(tmpdir(), '1111-probe-test-'));
  const requests = [];
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    requests.push(url.pathname);
    const html = options.blocked ? '<title>Loading...</title><body></body>'
      : options.empty ? '<title>搜尋頁</title><h1>採購</h1><p>目前沒有職缺</p>'
      : url.pathname === '/search/job' ? searchFixture(Number(url.searchParams.get('page')), options)
      : detailFixture(url.pathname.split('/').pop());
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
  });
  try {
    const report = await runProbe(context, { keyword: '採購', outputDir, timeoutMs: 800, pauseMs: 0 });
    assert.deepEqual(JSON.parse(await readFile(join(outputDir, 'report.json'), 'utf8')), report);
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
  assert.deepEqual(report.pages.map(p => p.jobs.map(j => j.id)), [['101'], ['202']]);
  assert.equal(report.pages[1].overlap, 0);
  assert.deepEqual(report.details.map(d => d.id), ['101', '202']);
  assert.match(report.details[0].text, /月薪 30,000/);
  assert.deepEqual(requests, ['/search/job', '/search/job', '/job/101', '/job/202']);
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

test('empty or hidden description does not borrow navbar or following-section text', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<nav>網站導覽</nav><h1>採購</h1><h2>工作內容</h2><p hidden>尚未載入的文字</p><p style="visibility:hidden">CSS隱藏文字</p><p style="opacity:0">透明文字</p><h2>工作待遇</h2><p>月薪</p>');
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
