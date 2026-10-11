import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readSearch, readDetail, searchUrl, validateSearch, validateDetail, evaluateWithTimeout, waitForValidDom, failureStatus } from './probe.mjs';

const ORIGIN = 'https://www.1111.com.tw';
const AREA_URL = `${ORIGIN}/api/v1/codeCategories/city`;
const failed = () => ({ status: 'failed', message: '1111 資料尚未完整載入或格式已變更，請稍後再試。' });
const unsupported = message => ({ status: 'unsupported', message });

function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('invalid input');
  if (input.action === 'search') {
    if (typeof input.keyword !== 'string' || !input.keyword.trim() || input.keyword.length > 200
        || !Array.isArray(input.area_names) || input.area_names.length > 10
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

export async function runSource(context, input, timeoutMs = 30000) {
  let page;
  let onResponse;
  try {
    validateInput(input);
    if (input.action === 'search' && input.page > 5) return unsupported('1111 實驗來源目前最多查詢前 5 頁。');
    if (input.action === 'search' && input.area_names.length > 1) return unsupported('1111 實驗來源目前只支援單一地區，請選擇一個縣市或行政區。');
    let area = '';
    if (input.action === 'search' && input.area_names.length) {
      const response = await context.request.get(AREA_URL, { timeout: Math.min(timeoutMs, 10000), headers: { 'Accept-Language': 'zh-TW' } });
      if (!response.ok()) return unsupported('1111 官方地區資料目前無法取得，請稍後再試。');
      area = resolveArea(input.area_names, await response.json());
      if (area === null) return unsupported('1111 官方地區資料無法唯一對應指定地區，請選擇其他單一縣市或行政區。');
    }
    page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(timeoutMs);
    page.setDefaultNavigationTimeout(timeoutMs);
    await page.bringToFront();
    if (input.action === 'search') {
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
      await page.goto(url.href, { waitUntil: 'domcontentloaded' });
      let metadata;
      const result = await waitForValidDom(page, readSearch, state => {
        metadata = responseResult?.page === input.page ? responseResult : state.initialResult;
        validateSearch(state, url.href, input.page, metadata, { allowEmpty: true });
        if (state.jobs.some(job => !/^\d{1,32}$/.test(job.id)
            || job.url !== `${ORIGIN}/job/${job.id}`
            || !/^https:\/\/www\.1111\.com\.tw\/corp\/\d{1,32}\/?$/.test(job.companyUrl))) throw new Error('invalid job identity');
      }, timeoutMs);
      return { status: 'ok', jobs: result.jobs, page: input.page, page_size: metadata.limit,
        total: metadata.totalCount, has_next: input.page * metadata.limit < metadata.totalCount,
        warnings: input.page === 5 && input.page * metadata.limit < metadata.totalCount ? ['1111 實驗來源目前最多查詢前 5 頁。'] : [] };
    }
    const job = { ...input.job, url: `${ORIGIN}/job/${input.job.id}` };
    await page.goto(job.url, { waitUntil: 'domcontentloaded' });
    const detail = await waitForValidDom(page, readDetail, state => validateDetail(state, job), timeoutMs);
    return { status: 'ok', detail: { ...detail, id: job.id, company: job.company, companyUrl: job.companyUrl } };
  } catch {
    if (page) {
      try {
        const state = await evaluateWithTimeout(page, () => ({ title: document.title, text: document.body.innerText.slice(0, 2000) }), Math.min(timeoutMs, 1000));
        if (failureStatus(state) === 'blocked_or_loading' || /驗證您是人類|確認您是人類|Verify you are human/i.test(state.text)) {
          return { status: 'blocked', message: '1111 目前停在載入或人機驗證，請稍後再試。' };
        }
      } catch { /* Do not expose browser diagnostics through the subprocess protocol. */ }
    }
    return failed();
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
  let context;
  let result;
  try {
    const { chromium } = await import('patchright');
    // Empty profile creates an isolated temporary session; no challenge clicks or retries.
    context = await chromium.launchPersistentContext('', { channel: 'chrome', headless: false, viewport: null, timeout: 15000 });
    result = await runSource(context, input);
  } catch {
    result = unsupported('1111 實驗來源需要已安裝 Chrome 與 Patchright 的桌面環境。');
  } finally {
    await context?.close().catch(() => {});
  }
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(() => { console.log(JSON.stringify(failed())); });
}
