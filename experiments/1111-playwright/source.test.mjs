import assert from 'node:assert/strict';
import { before, after, test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { chromium } from 'playwright';
import { runSource, resolveArea } from './source.mjs';
let browser;
before(async () => { browser = await chromium.launch(); });
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
  const context = await browser.newContext();
  const requests = [];
  let closed = false;
  const wrapper = { pages: () => context.pages(), newPage: () => context.newPage(),
    request: { get: async url => { assert.equal(url, 'https://www.1111.com.tw/api/v1/codeCategories/city'); return { ok: () => true, json: async () => catalogue }; } },
    close: async () => { closed = true; await context.close(); } };
  await context.route('**/*', route => {
    const url = new URL(route.request().url()); requests.push(url.href);
    const number = Number(url.searchParams.get('page'));
    const ids = options.empty ? [] : number === 4 ? ['701'] : ['501', '502'];
    const keyword = url.searchParams.get('ks');
    const search = `<title>搜尋職缺</title><h1>${keyword}</h1><input placeholder="工作職稱、公司名、技能專長..." value="${keyword}">
      ${(options.partial ? ids.slice(0, 1) : ids).map(id => `<div class="job-card__content"><a href="/job/${id}?mid=searchlist"><h2>測試職缺 ${id}</h2></a><a href="/corp/123"><h2>測試公司</h2></a></div>`).join('')}
      <script id="__NUXT_DATA__" type="application/json">${nuxt(number, ids, options.empty ? 0 : 7)}</script>`;
    const detail = `<h1>${options.wrongTitle ? '另一職缺' : '測試職缺 501'}</h1><a href="/corp/123">測試公司</a><h2>工作內容</h2><h3>職缺描述</h3><p>合成職缺說明，負責資料整理。</p><h3>工作待遇</h3><p>測試薪資</p>`;
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: options.blocked ? '<title>Loading...</title><body></body>' : url.pathname === '/search/job' ? search : detail });
  });
  const result = await runSource(wrapper, input, 400);
  assert.equal(closed, true);
  return { result, requests };
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
  for (const input of [{ ...search, area_names: ['不存在的城市'] }, { ...search, area_names: ['台北市', '台南市'] }, { ...search, page: 6 }]) {
    const { result, requests } = await fixture(input); assert.equal(result.status, 'unsupported'); assert.deepEqual(requests, []);
  }
});
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
