import assert from 'node:assert/strict';
import { test } from 'node:test';
import { serve, SINGLE_AREA_MS, STARTUP_MS, LAUNCH_TIMEOUT_MS, WORKER_GRACE_MS, MAX_AREAS, MAX_BUDGET_MS } from './source.mjs';

class Bus {
  constructor() { this.listeners = new Map(); }
  on(name, fn) { this.listeners.set(name, fn); return this; }
  once(name, fn) { this.listeners.set(name, fn); return this; }
  off(name) { this.listeners.delete(name); }
  emit(name, value) { this.listeners.get(name)?.(value); }
}
function page(state) {
  return {
    setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, isClosed: () => false,
    bringToFront: async () => {
      state.front += 1; state.events.push('front');
      if (state.focusFails) throw new Error('focus failed');
    }, on() {}, off() {},
    goto: async () => {
      state.events.push('request');
      if (state.navigationGate) await state.navigationGate;
      if (state.failNext) { state.failNext = false; state.lastFailure = true; throw new Error('blocked'); }
      state.lastFailure = false;
    },
    evaluate: async () => state.lastFailure ? { title: 'Verify you are human', text: 'Verify you are human' } : {
      url: 'https://www.1111.com.tw/search/job?ks=test&page=1', title: 'search', heading: 'test', keywordInput: 'test',
      jobs: [], initialResult: { page: 1, limit: 1, totalCount: 0, ids: [] },
    },
  };
}
function harness() {
  const stdin = new Bus(), signals = new Bus(), writes = [], state = { front: 0, closes: 0, failNext: false, lastFailure: false, events: [] };
  const stdout = { write(value, done) { state.events.push('write'); writes.push(JSON.parse(value)); done(); } };
  let launches = 0, context;
  const loadChromium = async () => ({ launchPersistentContext: async () => {
    launches += 1; const browser = new Bus(); const current = page(state);
    context = { pages: () => [current], newPage: async () => current, on: Bus.prototype.on, browser: () => browser,
      close: async () => { state.closes += 1; context.emit('close'); } };
    context.listeners = new Map(); context.emit = Bus.prototype.emit; context.close = context.close.bind(context);
    return context;
  } });
  return { stdin, signals, stdout, state, get launches() { return launches; }, get context() { return context; }, loadChromium, writes };
}
async function send(h, value) { h.stdin.emit('data', Buffer.from(value + '\r\n')); await new Promise(resolve => setImmediate(resolve)); }
const input = { action: 'search', keyword: 'test', area_names: [], page: 1 };

test('serve reuses one browser, preserves ids, and closes once at EOF', async () => {
  const h = harness(); const running = serve({ stdin: h.stdin, stdout: h.stdout, signals: h.signals, loadChromium: h.loadChromium });
  await send(h, JSON.stringify({ id: 'a', input })); await send(h, JSON.stringify({ id: 'b', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.launches, 1); assert.equal(h.state.front, 1); assert.equal(h.state.closes, 1);
  assert.deepEqual(h.writes.map(item => item.id), ['a', 'b']); assert.equal(h.writes[0].result.status, 'ok');
});

test('cold request focuses exactly once before navigation', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'cold', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.state.front, 1); assert.deepEqual(h.state.events, ['front', 'request', 'write']);
});

test('warm ok request does not focus', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'cold', input })); h.state.events.length = 0;
  await send(h, JSON.stringify({ id: 'warm', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.state.front, 1); assert.equal(h.writes[1].result.status, 'ok');
  assert.deepEqual(h.state.events, ['request', 'write']);
});

test('warm blocked request focuses immediately after result, including area batches', async () => {
  for (const areas of [[], ['A', 'B']]) {
    const h = harness(); const running = serve({ ...h });
    await send(h, JSON.stringify({ id: 'cold', input })); h.state.events.length = 0;
    h.context.request = { get: async () => ({ ok: () => true, json: async () => [
      { code: '100100', name: 'A', level: 2 }, { code: '100200', name: 'B', level: 2 },
    ] }) };
    h.state.failNext = true;
    await send(h, JSON.stringify({ id: 'blocked', input: { ...input, area_names: areas } }));
    h.stdin.emit('end'); await running;
    const result = h.writes[1].result;
    assert.equal(areas.length ? result.areas[0].status : result.status, 'blocked');
    assert.equal(h.state.front, 2); assert.deepEqual(h.state.events, ['request', 'front', 'write']);
  }
});

test('warm ok request following blocked does not focus again', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'cold', input }));
  h.state.failNext = true; await send(h, JSON.stringify({ id: 'blocked', input }));
  const fronts = h.state.front; h.state.events.length = 0;
  await send(h, JSON.stringify({ id: 'warm', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes[2].result.status, 'ok'); assert.equal(h.state.front, fronts);
  assert.deepEqual(h.state.events, ['request', 'write']);
});

test('blocked focus failure preserves public diagnostic', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'cold', input }));
  h.state.focusFails = true; h.state.failNext = true;
  await send(h, JSON.stringify({ id: 'blocked', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes[1].result.status, 'blocked'); assert.equal(h.state.front, 2);
});

test('serve relaunches after context closes between requests', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'cold', input }));
  h.context.emit('close'); await send(h, JSON.stringify({ id: 'again', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.launches, 2); assert.equal(h.state.front, 2); assert.equal(h.writes[1].id, 'again');
});

test('serve retries cold startup after a launch failure', async () => {
  const h = harness(); let attempts = 0;
  const running = serve({ ...h, loadChromium: async () => {
    if (++attempts === 1) throw new Error('launch failed');
    return h.loadChromium();
  } });
  await send(h, JSON.stringify({ id: 'failed', input }));
  await send(h, JSON.stringify({ id: 'again', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes[0].result.status, 'unsupported'); assert.equal(h.writes[1].result.status, 'ok');
  assert.equal(attempts, 2); assert.equal(h.state.closes, 1);
});

test('valid oversized JSON is rejected before launch and next request is processed', async () => {
  const h = harness(); const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'a1', input: { ...input, pad: 'x'.repeat(40000) } }));
  assert.equal(h.writes.length, 1); assert.equal(h.writes[0].result.status, 'failed'); assert.equal(h.launches, 0);
  await send(h, JSON.stringify({ id: 'ok', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes[1].id, 'ok'); assert.equal(h.writes[1].result.status, 'ok'); assert.equal(h.launches, 1);
});

for (const terminal of ['SIGTERM', 'SIGINT', 'end']) test(`${terminal} during request closes context and ends serve`, async () => {
  const h = harness(); let release;
  h.state.navigationGate = new Promise(resolve => { release = resolve; });
  const running = serve({ ...h });
  await send(h, JSON.stringify({ id: 'active', input }));
  assert.deepEqual(h.state.events, ['front', 'request']); assert.equal(h.writes.length, 0);
  if (terminal === 'end') h.stdin.emit('end'); else h.signals.emit(terminal);
  release(); await running;
  assert.equal(h.writes.length, terminal === 'end' ? 1 : 0); assert.equal(h.state.closes, 1);
  assert.equal(h.signals.listeners.size, 0); assert.equal(h.stdin.listeners.size, 0);
});

test('serve rejects a bad line and continues with the next request', async () => {
  const h = harness(); const running = serve({ stdin: h.stdin, stdout: h.stdout, signals: h.signals, loadChromium: h.loadChromium }); await send(h, 'not-json'); await send(h, JSON.stringify({ id: 'ok', input }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes[0].id, null); assert.equal(h.writes[0].result.message, '1111 \u67e5\u8a62\u53c3\u6578\u4e0d\u6b63\u78ba\u3002');
  assert.equal(h.writes[1].id, 'ok'); assert.equal(h.launches, 1);
});

const search = { action: 'search', keyword: '測試工作', area_names: [], page: 1 };

for (const importMs of [0, 4500, 5000]) test(`startup allowance covers ${importMs}ms import plus 15s launch before the first area`, async () => {
  // H2: imports plus a full 15s launch must fit the 20s startup allowance.
  let elapsed = 0, navigations = 0;
  const page = { setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, isClosed: () => false, bringToFront: async () => {}, on() {}, off() {},
    goto: async () => { navigations++; }, evaluate: async () => ({
      url: 'https://www.1111.com.tw/search/job?ks=測試工作&page=1', title: '搜尋職缺', heading: search.keyword, keywordInput: search.keyword,
      jobs: [{ id: '501', title: '合成職缺', company: '合成公司', url: 'https://www.1111.com.tw/job/501', companyUrl: 'https://www.1111.com.tw/corp/123' }],
      initialResult: { page: 1, limit: 1, totalCount: 1, ids: ['501'] } }) };
  const h = harness();
  const running = serve({ stdin: h.stdin, stdout: h.stdout, signals: h.signals,
    now: () => elapsed,
    loadChromium: async () => {
      elapsed += importMs;
      return { launchPersistentContext: async (profile, options) => {
        assert.equal(options.timeout, LAUNCH_TIMEOUT_MS);
        assert.equal(profile, '');
        assert.deepEqual(options, { channel: 'chrome', headless: false, viewport: null, timeout: LAUNCH_TIMEOUT_MS });
        elapsed += LAUNCH_TIMEOUT_MS;
        return { pages: () => [page], close: async () => {} };
      } };
    },
  });
  await send(h, JSON.stringify({ id: 'cold', input: { ...search, page: 1, budget_ms: STARTUP_MS + SINGLE_AREA_MS } }));
  h.stdin.emit('end'); await running;
  assert.equal(h.writes.length, 1); assert.equal(h.writes[0].id, 'cold');
  const result = h.writes[0].result;
  assert.equal(navigations, 1); assert.equal(result.status, 'ok'); assert.equal(result.jobs[0].id, '501');
  assert.equal(STARTUP_MS, 20000); assert.equal(LAUNCH_TIMEOUT_MS, 15000);
  assert.equal(MAX_BUDGET_MS, STARTUP_MS + SINGLE_AREA_MS * MAX_AREAS);
  assert.equal(MAX_BUDGET_MS, 470000);
  assert.equal(WORKER_GRACE_MS, 8000);
});

test('serve deducts cold startup time and keeps the next request warm', async () => {
  const h = harness(); let elapsed = 0;
  const running = serve({ stdin: h.stdin, stdout: h.stdout, signals: h.signals, now: () => elapsed,
    loadChromium: async () => { elapsed += STARTUP_MS + 1; return h.loadChromium(); } });
  const request = { ...input, budget_ms: STARTUP_MS + SINGLE_AREA_MS };
  await send(h, JSON.stringify({ id: 'cold', input: request }));
  await send(h, JSON.stringify({ id: 'warm', input: request }));
  h.stdin.emit('end'); await running;
  assert.equal(h.launches, 1);
  assert.equal(h.writes[0].result.areas[0].status, 'time_limited');
  assert.equal(h.writes[1].result.status, 'ok');
  assert.deepEqual(h.writes[1].result.jobs, []);
});
