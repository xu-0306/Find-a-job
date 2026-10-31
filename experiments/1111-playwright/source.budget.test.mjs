import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runSource, SINGLE_AREA_MS } from './source.mjs';
const search = { action: 'search', keyword: '測試工作', area_names: [], page: 1 };

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
