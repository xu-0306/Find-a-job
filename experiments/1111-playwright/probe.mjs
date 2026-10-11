import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';

const ORIGIN = 'https://www.1111.com.tw';

export function searchUrl(keyword, area = '') {
  if (!keyword?.trim()) throw new Error('keyword must not be empty');
  if (area && !/^\d{6}$/.test(area)) throw new Error('area must be a six-digit 1111 area code');
  const url = new URL('/search/job', ORIGIN);
  url.search = new URLSearchParams({ page: '1', ks: keyword.trim(), ...(area ? { c0: area } : {}) });
  return url.href;
}

// These functions run in the browser and read only rendered DOM, never session stores.
export function readSearch() {
  const jobs = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/job/"][href*="mid=searchlist"]')) {
    const url = new URL(a.getAttribute('href'), location.href);
    const id = url.pathname.match(/^\/job\/(\d+)\/?$/)?.[1];
    if (url.origin !== location.origin || !id || !a.checkVisibility({ visibilityProperty: true, opacityProperty: true }) || seen.has(id)) continue;
    const company = a.parentElement.querySelector('a[href^="/corp/"]');
    seen.add(id);
    jobs.push({
      id, url: `${url.origin}/job/${id}`, title: a.innerText.trim(),
      company: company?.querySelector('h2')?.innerText.trim() || company?.getAttribute('title') || '',
      companyUrl: company ? new URL(company.getAttribute('href'), location.href).href : '',
    });
  }
  return {
    url: location.href, title: document.title,
    heading: document.querySelector('h1')?.innerText.trim() || '',
    keywordInput: [...document.querySelectorAll('input[placeholder="工作職稱、公司名、技能專長..."]')]
      .find(e => e.checkVisibility({ visibilityProperty: true, opacityProperty: true }))?.value || '',
    jobs,
  };
}

export function validateSearch(page, expected, pageNumber) {
  const actual = new URL(page.url);
  const wanted = new URL(expected);
  const mismatch = actual.origin !== wanted.origin || actual.pathname !== wanted.pathname
    || actual.searchParams.get('page') !== String(pageNumber)
    || actual.searchParams.get('ks') !== wanted.searchParams.get('ks')
    || (actual.searchParams.get('c0') || '') !== (wanted.searchParams.get('c0') || '')
    || page.keywordInput !== wanted.searchParams.get('ks')
    || !page.heading.includes(wanted.searchParams.get('ks'));
  if (mismatch) throw new Error('query_mismatch: URL, heading or search input does not match the requested query');
  if (!page.jobs.length) throw new Error('layout_changed_or_empty: no verified search cards');
  if (page.jobs.some(job => !job.title || !job.company || !job.companyUrl)) {
    throw new Error('incomplete_search_card: missing title or company');
  }
}

export function readDetail() {
  const headings = [...document.querySelectorAll('h1,h2,h3,h4')]
    .filter(h => h.checkVisibility({ visibilityProperty: true, opacityProperty: true }));
  const index = headings.findIndex(h => h.innerText.trim() === '工作內容');
  const description = [];
  if (index >= 0 && headings[index + 1]) {
    const range = document.createRange();
    range.setStartAfter(headings[index]);
    range.setEndBefore(headings[index + 1]);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (range.intersectsNode(node) && node.parentElement.checkVisibility({ visibilityProperty: true, opacityProperty: true })
          && !node.parentElement.closest('script,style,noscript')) description.push(node.textContent.trim());
    }
  }
  return {
    url: location.href, title: document.querySelector('h1')?.innerText.trim() || '',
    companyUrls: [...document.querySelectorAll('a[href^="/corp/"]')]
      .filter(a => a.checkVisibility({ visibilityProperty: true, opacityProperty: true }))
      .map(a => new URL(a.getAttribute('href'), location.href).href),
    // Keep the visible detail as evidence; normalization belongs to a later product integration.
    text: document.body.innerText.trim(),
    // A bounded section avoids accepting navbar/footer text when the description is still empty.
    description: description.filter(Boolean).join('\n'),
  };
}

export function validateDetail(detail, job) {
  const actual = new URL(detail.url);
  const wanted = new URL(job.url);
  if (actual.origin !== wanted.origin || actual.pathname.replace(/\/$/, '') !== wanted.pathname
      || detail.title !== job.title || !detail.companyUrls.includes(job.companyUrl)) {
    throw new Error('detail_identity_mismatch: job URL, title or company differs from its search card');
  }
  if (!detail.description.trim() || /^(Loading\.{3}|載入中[.。…]*)$/i.test(detail.description.trim())) {
    throw new Error('incomplete_detail: readable job description not confirmed');
  }
}

export function failureStatus(state) {
  // Challenge libraries also appear on working pages; their mere presence is not evidence of blocking.
  return !state.text.trim() && (/^Loading\.{3}$/i.test(state.title) || state.title.startsWith('⏳'))
    ? 'blocked_or_loading' : 'failed';
}

async function waitForValidDom(page, read, validate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const result = await page.evaluate(read);
      validate(result);
      return result;
    } catch (error) {
      if (Date.now() >= deadline) throw error;
      await delay(Math.min(250, deadline - Date.now()));
    }
  }
}

export async function runProbe(context, { keyword, area = '', outputDir, timeoutMs = 30000, pauseMs = 1500 }) {
  const expected = searchUrl(keyword, area);
  await mkdir(outputDir, { recursive: true });
  const page = await context.newPage();
  page.setDefaultTimeout(timeoutMs);
  page.setDefaultNavigationTimeout(timeoutMs);
  const report = {
    startedAt: new Date().toISOString(), query: { keyword: keyword.trim(), area },
    status: 'running', stages: { search: 'not_run', pagination: 'not_run', details: 'not_run' },
    pages: [], details: [],
  };
  let stage = 'search';
  try {
    await page.goto(expected, { waitUntil: 'domcontentloaded' });
    for (let number = 1; number <= 2; number++) {
      if (number === 2) {
        stage = 'pagination';
        await delay(pauseMs);
        // The current site exposes duplicate desktop/mobile controls; click the visible one.
        await page.locator('button.pagination-button[title="前往2頁"]:visible').first().click();
        await page.waitForURL(url => url.searchParams.get('page') === '2', { waitUntil: 'domcontentloaded' });
      }
      const result = await waitForValidDom(page, readSearch, result => {
        validateSearch(result, expected, number);
        if (number === 2) {
          const previousIds = new Set(report.pages[0].jobs.map(job => job.id));
          result.overlap = result.jobs.filter(job => previousIds.has(job.id)).length;
          if (result.overlap === result.jobs.length) throw new Error('pagination_unchanged: page 2 has no new job IDs');
        }
      }, timeoutMs);
      report.pages.push(result);
      report.stages[stage] = 'passed';
    }
    stage = 'details';
    const sample = [report.pages[0].jobs[0], report.pages[1].jobs.find(job =>
      !report.pages[0].jobs.some(previous => previous.id === job.id))];
    for (const job of sample) {
      await delay(pauseMs);
      await page.goto(job.url, { waitUntil: 'domcontentloaded' });
      const detail = await waitForValidDom(page, readDetail, detail => validateDetail(detail, job), timeoutMs);
      report.details.push({ id: job.id, company: job.company, ...detail });
    }
    report.stages.details = 'passed';
    report.status = 'passed';
  } catch (error) {
    report.stages[stage] = 'failed';
    report.status = 'failed';
    report.error = { stage, message: error.message };
    try {
      const state = await page.evaluate(() => ({ url: location.href, title: document.title, text: document.body.innerText.slice(0, 2000) }));
      report.status = failureStatus(state);
      report.diagnostic = state;
      await page.screenshot({ path: resolve(outputDir, 'failure.png'), timeout: 5000 });
    } catch (diagnosticError) {
      report.diagnosticError = diagnosticError.message;
    }
  } finally {
    report.finishedAt = new Date().toISOString();
    await writeFile(resolve(outputDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    await page.close();
  }
  return report;
}

async function main() {
  const { values } = parseArgs({ options: {
    keyword: { type: 'string', default: '採購' }, area: { type: 'string', default: '' },
    headed: { type: 'boolean', default: false }, chrome: { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  } });
  if (values.help) {
    console.log('npm run probe -- [--keyword 採購] [--area 101600] [--headed] [--chrome]\nChecks two search pages and two details. Stops on failure; writes output/playwright/<timestamp>/report.json.');
    return;
  }
  searchUrl(values.keyword, values.area);
  const outputDir = resolve('output/playwright', new Date().toISOString().replace(/[:.]/g, '-'));
  let browser;
  try {
    browser = await chromium.launch({ headless: !values.headed, channel: values.chrome ? 'chrome' : 'chromium' });
    const context = await browser.newContext({ locale: 'zh-TW', viewport: { width: 1365, height: 900 } });
    const report = await runProbe(context, { keyword: values.keyword, area: values.area, outputDir });
    console.log(JSON.stringify({ status: report.status, stages: report.stages, outputDir }, null, 2));
    process.exitCode = report.status === 'passed' ? 0 : 1;
  } finally {
    await browser?.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
