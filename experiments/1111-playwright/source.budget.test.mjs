import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runSource, runWorker, SINGLE_AREA_MS, STARTUP_MS, LAUNCH_TIMEOUT_MS, WORKER_GRACE_MS, MAX_AREAS, MAX_BUDGET_MS } from './source.mjs';
const search = { action: 'search', keyword: '測試工作', area_names: [], page: 1 };

for (const importMs of [0, 4500, 5000]) test(`startup allowance covers ${importMs}ms import plus 15s launch before the first area`, async () => {
  // H2: imports plus a full 15s launch must fit the 20s startup allowance.
  let elapsed = 0, navigations = 0;
  const page = { setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, bringToFront: async () => {}, on() {}, off() {},
    goto: async () => { navigations++; }, evaluate: async () => ({
      url: 'https://www.1111.com.tw/search/job?ks=測試工作&page=1', title: '搜尋職缺', heading: search.keyword, keywordInput: search.keyword,
      jobs: [{ id: '501', title: '合成職缺', company: '合成公司', url: 'https://www.1111.com.tw/job/501', companyUrl: 'https://www.1111.com.tw/corp/123' }],
      initialResult: { page: 1, limit: 1, totalCount: 1, ids: ['501'] } }) };
  const result = await runWorker({ ...search, page: 1, budget_ms: STARTUP_MS + SINGLE_AREA_MS }, {
    now: () => elapsed,
    loadChromium: async () => {
      elapsed += importMs;
      return { launchPersistentContext: async (profile, options) => {
        assert.equal(options.timeout, LAUNCH_TIMEOUT_MS); elapsed += LAUNCH_TIMEOUT_MS;
        return { pages: () => [page], close: async () => {} };
      } };
    },
  });
  assert.equal(navigations, 1); assert.equal(result.status, 'ok'); assert.equal(result.jobs[0].id, '501');
  assert.equal(STARTUP_MS, 20000); assert.equal(LAUNCH_TIMEOUT_MS, 15000);
  assert.equal(MAX_BUDGET_MS, STARTUP_MS + SINGLE_AREA_MS * MAX_AREAS);
  assert.equal(MAX_BUDGET_MS, 470000);
  assert.equal(WORKER_GRACE_MS, 8000);
});

test('expired area still diagnoses a challenge within the total deadline', async () => {
  // Without F4 diagnostic evaluate gets 1ms and loses this challenge result arriving after 20ms.
  let elapsed = 0, reads = 0;
  const page = { setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, bringToFront: async () => {}, on() {}, off() {},
    goto: async () => { elapsed = SINGLE_AREA_MS; throw new Error('area navigation timeout'); },
    evaluate: async () => { reads++; await new Promise(resolve => setTimeout(resolve, 20));
      return { title: 'Verify you are human', text: 'Verify you are human' }; } };
  const result = await runSource({ pages: () => [page], close: async () => {} },
    { ...search, budget_ms: SINGLE_AREA_MS + 1000 }, 30000, () => elapsed);
  assert.equal(result.status, 'blocked'); assert.equal(reads, 1);
  // Use a separate exhausted clock to verify no diagnostic can exceed the total budget.
  elapsed = 0; reads = 0;
  const totalExpired = await runSource({ pages: () => [page], close: async () => {} },
    { ...search, budget_ms: SINGLE_AREA_MS }, 30000, () => elapsed);
  assert.equal(totalExpired.status, 'failed'); assert.equal(reads, 0);
});
