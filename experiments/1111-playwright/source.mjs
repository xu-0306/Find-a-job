import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readSearch, readDetail, searchUrl, validateSearch, validateDetail, evaluateWithTimeout, waitForValidDom, failureStatus } from './probe.mjs';

const ORIGIN = 'https://www.1111.com.tw';
const AREA_URL = `${ORIGIN}/api/v1/codeCategories/city`;
const optionalSearchField = value => typeof value === 'string' && value.trim() && value.length <= 500 ? value.trim() : undefined;
const apiFieldsFor = hit => {
  const city = hit?.workCity;
  return {
    id: String(hit?.jobId),
    ...(optionalSearchField(city?.name) ? { location: optionalSearchField(city.name) } : {}),
    ...(optionalSearchField(hit?.salary) ? { salary: optionalSearchField(hit.salary) } : {}),
  };
};
const failed = () => ({ status: 'failed', message: '1111 資料尚未完整載入或格式已變更，請稍後再試。' });
const unsupported = message => ({ status: 'unsupported', message });
// Matches Python SINGLE_AREA_SECONDS: navigation (30s) plus DOM validation (15s).
export const SINGLE_AREA_MS = 45000;
// Mirrors app/source1111.py; the consistency regression test locks units and formulas.
export const STARTUP_MS = 20000;
export const LAUNCH_TIMEOUT_MS = 15000;
export const WORKER_GRACE_MS = 8000;
export const MAX_AREAS = 10;
export const MAX_SEARCH_PAGES = 5;
export const MAX_BUDGET_MS = STARTUP_MS + SINGLE_AREA_MS * MAX_AREAS;
const workerBudget = input => input.budget_ms ?? STARTUP_MS + SINGLE_AREA_MS * Math.max(1, input.area_names?.length || 0);
const timeLimited = () => ({ status: 'time_limited', message: '時間不足，此地區未查；可按續查補查。' });

export function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('invalid input');
  if (input.budget_ms !== undefined && (!Number.isSafeInteger(input.budget_ms)
      || input.budget_ms <= 0 || input.budget_ms > MAX_BUDGET_MS)) throw new Error('invalid budget');
  if (input.action === 'search') {
    if (typeof input.keyword !== 'string' || !input.keyword.trim() || input.keyword.length > 200
        || !Array.isArray(input.area_names) || input.area_names.length > MAX_AREAS
        || input.area_names.some(name => typeof name !== 'string' || !name.trim() || name.length > 100)
        || !Number.isSafeInteger(input.page) || input.page < 1) throw new Error('invalid search');
  } else if (input.action === 'detail') {
    const job = input.job;
    if (!job || typeof job.id !== 'string' || !/^\d{1,32}$/.test(job.id)
        || typeof job.title !== 'string' || !job.title.trim() || job.title.length > 500
        || typeof job.company !== 'string' || !job.company.trim() || job.company.length > 500
        || !new RegExp(`^https://www\\.1111\\.com\\.tw/job/${job.id}/?$`).test(job.url)
        || typeof job.companyUrl !== 'string' || !/^https:\/\/www\.1111\.com\.tw\/corp\/\d{1,32}\/?$/.test(job.companyUrl)) {
      throw new Error('invalid detail');
    }
  } else throw new Error('invalid action');
}

// Resolve exact full names using the same official catalogue as the site's region selector.
export function resolveArea(names, catalogue) {
  if (!names.length) return '';
  if (names.length !== 1) return null;
  if (!Array.isArray(catalogue)) return null;
  const byCode = new Map(catalogue.map(row => [String(row.code), row]));
  const normalize = name => name.trim().replaceAll('臺', '台');
  const matches = [];
  for (const row of catalogue) {
    if (![2, 3].includes(row.level) || !/^\d{6}$/.test(String(row.code)) || typeof row.name !== 'string') continue;
    let fullName = row.name;
    if (row.level === 3) {
      const parent = byCode.get(String(row.parentCode));
      if (!parent || parent.level !== 2 || typeof parent.name !== 'string') continue;
      fullName = parent.name + fullName;
    }
    if (normalize(fullName) === normalize(names[0])) matches.push(String(row.code));
  }
  return matches.length === 1 ? matches[0] : null;
}

export async function runSource(context, input, timeoutMs = 30000, now = () => performance.now()) {
  const deadline = now() + workerBudget(input);
  let areaDeadline = deadline;
  const remaining = () => Math.max(1, Math.floor(areaDeadline - now()));
  const operationTimeout = (cap = timeoutMs) => Math.min(cap, remaining());
  let page;
  let onResponse;
  async function failure() {
    const diagnosticMs = Math.min(1000, Math.floor(deadline - now()));
    if (page && diagnosticMs > 0) {
      try {
        const state = await evaluateWithTimeout(page, () => ({ title: document.title, text: document.body.innerText.slice(0, 2000) }), diagnosticMs);
        if (failureStatus(state) === 'blocked_or_loading' || /驗證您是人類|確認您是人類|Verify you are human/i.test(state.text)) {
          return { status: 'blocked', message: '1111 目前停在載入或人機驗證，請稍後再試。' };
        }
      } catch { /* Never expose raw browser diagnostics. */ }
    }
    return failed();
  }
  try {
    validateInput(input);
    if (input.action === 'search' && input.page > MAX_SEARCH_PAGES) return unsupported('1111 實驗來源目前最多查詢前 5 頁。');
    let catalogue;
    if (input.action === 'search' && input.area_names.length) {
      const response = await context.request.get(AREA_URL, { timeout: operationTimeout(Math.min(timeoutMs, 10000)), headers: { 'Accept-Language': 'zh-TW' } });
      if (!response.ok()) return unsupported('1111 官方地區資料目前無法取得，請稍後再試。');
      catalogue = await response.json();
    }
    page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(operationTimeout());
    page.setDefaultNavigationTimeout(operationTimeout());
    if (input.action === 'search') {
      const results = [];
      let blocked = false;
      for (const name of input.area_names.length ? input.area_names : ['']) {
        const area = name ? resolveArea([name], catalogue) : '';
        let result;
        if (deadline - now() < SINGLE_AREA_MS) {
          result = timeLimited();
        } else if (area === null) {
          result = unsupported('1111 官方地區資料無法唯一對應指定地區，請選擇其他縣市或行政區。');
        } else if (blocked) {
          result = { status: 'blocked', message: '1111 同一瀏覽器已遇到人機驗證，此地區未查，請稍後再試。' };
        } else {
          areaDeadline = Math.min(deadline, now() + SINGLE_AREA_MS);
          page.setDefaultTimeout(operationTimeout());
          page.setDefaultNavigationTimeout(operationTimeout());
          try {
            result = await searchPage(area);
          } catch {
            result = await failure();
            blocked = result.status === 'blocked';
          } finally {
            if (onResponse) page.off('response', onResponse);
            onResponse = undefined;
          }
        }
        results.push({ area_name: name, ...result });
      }
      // One/no area keeps the original payload; batches contain that same payload per named area.
      if (input.area_names.length <= 1 && results[0].status !== 'time_limited') {
        const { area_name, ...result } = results[0];
        return result;
      }
      return { status: 'ok', areas: results };
    }
    const job = { ...input.job, url: `${ORIGIN}/job/${input.job.id}` };
    await page.goto(job.url, { waitUntil: 'domcontentloaded', timeout: operationTimeout() });
    const detail = await waitForValidDom(page, readDetail, state => validateDetail(state, job), operationTimeout());
    return { status: 'ok', detail: { ...detail, id: job.id, company: job.company, companyUrl: job.companyUrl } };

    async function searchPage(area) {
      const url = new URL(searchUrl(input.keyword, area));
      url.searchParams.set('page', String(input.page));
      let responseResult;
      onResponse = async response => {
        try {
          const responseUrl = new URL(response.url());
          if (responseUrl.origin !== ORIGIN || responseUrl.pathname !== '/api/v1/search/jobs/' || !response.ok()) return;
          const { result } = await response.json();
          responseResult = { ...result.pagination, ids: result.hits.map(job => String(job.jobId)),
            fields: result.hits.map(apiFieldsFor), source: 'search-response' };
        } catch { responseResult = undefined; }
      };
      page.on('response', onResponse);
      await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: operationTimeout() });
      let metadata;
      const result = await waitForValidDom(page, readSearch, state => {
        metadata = responseResult?.page === input.page ? responseResult : state.initialResult;
        validateSearch(state, url.href, input.page, metadata, { allowEmpty: true });
        if (state.jobs.some(job => !/^\d{1,32}$/.test(job.id)
            || job.url !== `${ORIGIN}/job/${job.id}`
            || !/^https:\/\/www\.1111\.com\.tw\/corp\/\d{1,32}\/?$/.test(job.companyUrl))) throw new Error('invalid job identity');
      }, operationTimeout());
      const fieldsById = new Map((metadata.fields || []).map(field => [field.id, field]));
      const jobs = result.jobs.map(job => {
        const fields = fieldsById.get(job.id);
        return fields ? { ...job, ...(fields.location ? { location: fields.location } : {}),
          ...(fields.salary ? { salary: fields.salary } : {}) } : job;
      });
      return { status: 'ok', jobs, page: input.page, page_size: metadata.limit,
        total: metadata.totalCount, has_next: input.page * metadata.limit < metadata.totalCount,
        warnings: input.page === MAX_SEARCH_PAGES && input.page * metadata.limit < metadata.totalCount ? ['1111 實驗來源目前最多查詢前 5 頁。'] : [] };
    }
  } catch {
    return await failure();
  } finally {
    if (onResponse) page?.off('response', onResponse);
  }
}

// One isolated browser belongs to this server, never to an individual request.
export async function serve({ stdin = process.stdin, stdout = process.stdout, signals = process,
    loadChromium = async () => (await import('patchright')).chromium, now = () => performance.now() } = {}) {
  let context, launching;
  let invalid = false, closing = false;
  let pending = Promise.resolve();
  let raw = Buffer.alloc(0), oversized = false;
  const invalidInput = () => ({ status: 'failed', message: '1111 \u67e5\u8a62\u53c3\u6578\u4e0d\u6b63\u78ba\u3002' });
  const validId = id => typeof id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(id);
  const bounded = async (work, milliseconds) => {
    let timer;
    try {
      await Promise.race([work, new Promise(resolve => { timer = setTimeout(resolve, milliseconds); })]);
    } finally { clearTimeout(timer); }
  };
  async function closeContext() {
    const owned = context;
    context = undefined;
    if (owned) await owned.close().catch(() => { /* Never expose raw browser diagnostics. */ });
  }
  async function request(line, tooLong) {
    if (closing) return;
    let id = null, input, result;
    try {
      const envelope = JSON.parse(line.toString('utf8'));
      if (validId(envelope?.id)) id = envelope.id;
      if (tooLong || id === null) throw new Error('invalid envelope');
      input = envelope.input;
      validateInput(input);
    } catch { result = invalidInput(); }
    if (!result) {
      const started = now();
      const cold = !context || invalid;
      let starting = cold;
      try {
        if (cold) {
          await closeContext();
          invalid = false;
          launching = (async () => {
            const chromium = await loadChromium();
            // Empty profile isolates this worker; no personal cookies, challenge clicks or retries.
            context = await chromium.launchPersistentContext('', { channel: 'chrome', headless: false, viewport: null, timeout: LAUNCH_TIMEOUT_MS });
            const owned = context;
            const invalidate = () => { if (context === owned) invalid = true; };
            owned.on?.('close', invalidate);
            owned.browser?.()?.on?.('disconnected', invalidate);
          })();
          try { await launching; } finally { launching = undefined; }
        }
        starting = false;
        let page = context.pages()[0];
        if (!page || page.isClosed()) page = await context.newPage();
        if (cold) await page.bringToFront();
        const budget = workerBudget(input) - (cold ? now() - started : 0);
        result = await runSource(context, { ...input, budget_ms: Math.max(1, Math.floor(budget)) }, 30000, now);
        if (invalid) result = failed();
        if (result.status === 'blocked' || result.areas?.some(area => area.status === 'blocked')) {
          await page.bringToFront().catch(() => { /* Focus is best effort; preserve the diagnostic. */ });
        }
      } catch {
        invalid = true;
        result = starting ? unsupported('1111 實驗來源需要已安裝 Chrome 與 Patchright 的桌面環境。') : failed();
      }
    }
    if (!closing) await new Promise((resolve, reject) => {
      stdout.write(JSON.stringify({ id, result }) + '\n', error => error ? reject(error) : resolve());
    });
  }
  function enqueue() {
    const line = raw, tooLong = oversized;
    raw = Buffer.alloc(0); oversized = false;
    pending = pending.then(() => request(line, tooLong));
    // Attach the rejection handler immediately; transport errors end service safely.
    pending.catch(failTransport);
  }
  function receive(chunk) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    let start = 0;
    for (let index = 0; index <= bytes.length; index++) {
      if (index < bytes.length && bytes[index] !== 10) continue;
      const part = bytes.subarray(start, index);
      if (raw.length + part.length + 1 > 32768) oversized = true;
      if (!oversized) raw = Buffer.concat([raw, part]);
      if (index < bytes.length) enqueue();
      start = index + 1;
    }
  }
  let finish, failTransport;
  const ended = new Promise((resolve, reject) => { finish = resolve; failTransport = reject; });
  const eof = () => { if (raw.length || oversized) enqueue(); finish(true); };
  const signal = () => { closing = true; finish(false); };
  stdin.on('data', receive);
  stdin.once('end', eof);
  stdin.once('error', failTransport);
  signals.on('SIGTERM', signal);
  signals.on('SIGINT', signal);
  try {
    await ended;
    // EOF normally drains queued requests; a dead parent cannot leave an active browser indefinitely.
    await bounded((async () => {
      await pending;
      closing = true;
      await closeContext();
    })(), WORKER_GRACE_MS);
  } finally {
    closing = true;
    stdin.off('data', receive);
    stdin.off('end', eof);
    stdin.off('error', failTransport);
    signals.off('SIGTERM', signal);
    signals.off('SIGINT', signal);
    await bounded((async () => { if (launching) await launching; await closeContext(); })(), WORKER_GRACE_MS);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv[2] === '--serve') {
    serve().then(() => process.exit(0), error => { console.error(error); process.exit(1); });
  } else {
    console.error('Usage: node source.mjs --serve');
    process.exitCode = 1;
  }
}
