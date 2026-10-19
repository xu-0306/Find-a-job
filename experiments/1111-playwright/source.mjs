import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readSearch, readDetail, searchUrl, validateSearch, validateDetail, evaluateWithTimeout, waitForValidDom, failureStatus } from './probe.mjs';

const ORIGIN = 'https://www.1111.com.tw';
const AREA_URL = `${ORIGIN}/api/v1/codeCategories/city`;
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
    await page.bringToFront();
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
          responseResult = { ...result.pagination, ids: result.hits.map(job => String(job.jobId)) };
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
      return { status: 'ok', jobs: result.jobs, page: input.page, page_size: metadata.limit,
        total: metadata.totalCount, has_next: input.page * metadata.limit < metadata.totalCount,
        warnings: input.page === MAX_SEARCH_PAGES && input.page * metadata.limit < metadata.totalCount ? ['1111 實驗來源目前最多查詢前 5 頁。'] : [] };
    }
  } catch {
    return await failure();
  } finally {
    if (onResponse) page?.off('response', onResponse);
    await context.close().catch(() => {});
  }
}

async function main() {
  let input;
  try {
    process.stdin.setEncoding('utf8');
    let raw = '';
    for await (const chunk of process.stdin) {
      raw += chunk;
      if (Buffer.byteLength(raw, 'utf8') > 32768) throw new Error('oversized input');
    }
    input = JSON.parse(raw);
    validateInput(input);
  } catch {
    console.log(JSON.stringify({ status: 'failed', message: '1111 查詢參數不正確。' }));
    return;
  }
  console.log(JSON.stringify(await runWorker(input)));
}

export async function runWorker(input, { loadChromium = async () => (await import('patchright')).chromium,
    now = () => performance.now() } = {}) {
  const started = now();
  let context;
  let result;
  try {
    const chromium = await loadChromium();
    // Empty profile creates an isolated temporary session; no challenge clicks or retries.
    context = await chromium.launchPersistentContext('', { channel: 'chrome', headless: false, viewport: null, timeout: LAUNCH_TIMEOUT_MS });
    const budget = workerBudget(input);
    result = await runSource(context, { ...input, budget_ms: Math.max(1, Math.floor(budget - (now() - started))) }, 30000, now);
  } catch {
    result = unsupported('1111 實驗來源需要已安裝 Chrome 與 Patchright 的桌面環境。');
  } finally {
    await context?.close().catch(() => {});
  }
  return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(() => { console.log(JSON.stringify(failed())); });
}
