import { mkdir, rmdir, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { runProbe, searchUrl } from './probe.mjs';

const OUTPUT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), 'output/playwright');
const SESSION_CODES = new Set(['invalid_arguments', 'profile_busy', 'profile_lock_failed', 'launch_failed',
  'probe_failed', 'round_failed', 'close_failed', 'interrupted', 'output_failed', 'session_failed']);
const PROBE_CODES = new Set(['query_mismatch', 'layout_changed_or_empty', 'incomplete_search_card',
  'incomplete_page', 'pagination_unchanged', 'detail_identity_mismatch', 'incomplete_detail',
  'dom_read_timeout', 'timeout', 'browser_error']);
const failure = code => Object.assign(new Error(code), { code });
const safeCode = (code, fallback) => (SESSION_CODES.has(code) || PROBE_CODES.has(code)) ? code : fallback;

async function launch(profilePath, options) {
  const { chromium } = await import('patchright');
  return chromium.launchPersistentContext(profilePath, options);
}

export async function runSession({ keyword = '採購', area = '', rounds = 3 } = {}, {
  launch: launchContext = launch, probe = runProbe, outputRoot = OUTPUT_ROOT,
  roundDelayMs = 20000, timeoutMs = 30000, pauseMs = 1500, runtime = {}, signalSource = process,
} = {}) {
  if (!Number.isInteger(rounds) || rounds < 1 || rounds > 3) throw failure('invalid_arguments');
  try { searchUrl(keyword, area); } catch { throw failure('invalid_arguments'); }
  const profilePath = resolve(outputRoot, 'session-profile');
  const lockPath = `${profilePath}.lock`;
  try { await mkdir(outputRoot, { recursive: true }); } catch { throw failure('output_failed'); }
  // shortcut: abrupt process termination leaves this lock, inspect the dedicated browser before manual removal.
  try { await mkdir(lockPath); } catch (error) {
    throw failure(error.code === 'EEXIST' ? 'profile_busy' : 'profile_lock_failed');
  }
  const started = Date.now();
  const outputDir = resolve(outputRoot, `session-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID()}`);
  const summary = {
    startedAt: new Date(started).toISOString(), status: 'running', requestedRounds: rounds,
    stages: { launch: 'not_run', round: 'not_run', close: 'not_run' }, events: [], rounds: [],
  };
  let context;
  let closePromise;
  let closeFailed = false;
  let interrupted = false;
  const abort = new AbortController();
  const close = () => {
    if (!context) return Promise.resolve();
    if (!closePromise) {
      const closeStarted = Date.now();
      closePromise = Promise.resolve().then(() => context.close()).then(() => {
        summary.stages.close = 'passed';
      }, () => {
        closeFailed = true;
        summary.stages.close = 'failed';
        summary.status = 'failed';
        summary.error = { code: 'close_failed' };
      }).finally(() => {
        summary.events.push({ phase: 'close', status: summary.stages.close, elapsedMs: Date.now() - closeStarted });
      });
    }
    return closePromise;
  };
  const onSignal = () => {
    interrupted = true;
    abort.abort();
    void close();
  };
  signalSource.on('SIGINT', onSignal);
  signalSource.on('SIGTERM', onSignal);
  try {
    await mkdir(outputDir);
    const launchStarted = Date.now();
    try {
      context = await launchContext(profilePath, { channel: 'chrome', headless: false, viewport: null });
      summary.stages.launch = 'passed';
    } catch {
      summary.stages.launch = 'failed';
      summary.status = 'failed';
      summary.error = { code: 'launch_failed' };
    }
    summary.events.push({ phase: 'launch', status: summary.stages.launch, elapsedMs: Date.now() - launchStarted });
    for (let round = 1; context && !interrupted && round <= rounds; round++) {
      const roundStarted = Date.now();
      const reportPath = `round-${round}/report.json`;
      let report;
      try {
        report = await probe(context, {
          keyword, area, outputDir: resolve(outputDir, `round-${round}`), timeoutMs, pauseMs,
          runtime: {
            driver: 'patchright', driverVersion: createRequire(import.meta.url)('patchright/package.json').version,
            browserVersion: context.browser?.()?.version(), channel: 'chrome', headed: true,
            context: 'dedicated-persistent', ...runtime,
          },
          closePage: false,
        });
      } catch {
        report = { status: 'failed', pages: [], details: [], error: { code: 'probe_failed' } };
        await mkdir(resolve(outputDir, `round-${round}`), { recursive: true });
        await writeFile(resolve(outputDir, reportPath), `${JSON.stringify(report, null, 2)}\n`);
      }
      const status = ['passed', 'failed', 'blocked_or_loading'].includes(report?.status) ? report.status : 'failed';
      const result = {
        round, status, elapsedMs: Date.now() - roundStarted, reportPath,
        counts: { pages: report?.pages?.length || 0, details: report?.details?.length || 0,
          jobs: report?.pages?.reduce((count, page) => count + (page.jobs?.length || 0), 0) || 0 },
      };
      if (status !== 'passed') result.error = { code: safeCode(report?.error?.code, 'round_failed') };
      summary.rounds.push(result);
      summary.events.push({ phase: 'round', round, status, elapsedMs: result.elapsedMs, ...(result.error ? { error: result.error } : {}) });
      summary.stages.round = status;
      if (status !== 'passed') {
        summary.status = status;
        summary.error = result.error;
        break;
      }
      if (round < rounds && !interrupted) {
        try { await delay(roundDelayMs, undefined, { signal: abort.signal }); } catch {
          if (!interrupted) throw failure('session_failed');
        }
      }
    }
    if (interrupted) {
      summary.status = 'interrupted';
      summary.error = { code: 'interrupted' };
    } else if (summary.status === 'running') summary.status = 'passed';
  } catch (error) {
    summary.status = 'failed';
    summary.error = { code: safeCode(error.code, 'output_failed') };
  } finally {
    await close();
    signalSource.off('SIGINT', onSignal);
    signalSource.off('SIGTERM', onSignal);
    if (closeFailed) {
      summary.status = 'failed';
      summary.error = { code: 'close_failed' };
    } else if (interrupted) {
      summary.status = 'interrupted';
      summary.error = { code: 'interrupted' };
    }
    // A failed close may leave Chrome using the profile; keep protection in place.
    if (!closeFailed) {
      try { await rmdir(lockPath); } catch {
        summary.status = 'failed';
        summary.error = { code: 'profile_lock_failed' };
      }
    }
    summary.finishedAt = new Date().toISOString();
    summary.elapsedMs = Date.now() - started;
    try {
      await writeFile(resolve(outputDir, 'session-summary.json'), `${JSON.stringify(summary, null, 2)}\n`);
    } catch { summary.status = 'failed'; summary.error = { code: 'output_failed' }; }
  }
  return { ...summary, outputDir };
}

export async function runCli(args = process.argv.slice(2), dependencies = {}, io = console) {
  try {
    const { values } = parseArgs({ args, options: {
      keyword: { type: 'string', default: '採購' }, area: { type: 'string', default: '' },
      rounds: { type: 'string', default: '3' }, help: { type: 'boolean', default: false },
    } });
    if (values.help) {
      io.log('node session.mjs [--keyword 採購] [--area 101600] [--rounds 1..3]\nUses dedicated output/playwright/session-profile; two pages and two details per round, 20 seconds between rounds.');
      return 0;
    }
    if (!/^[1-3]$/.test(values.rounds)) throw failure('invalid_arguments');
    const summary = await runSession({ keyword: values.keyword, area: values.area, rounds: Number(values.rounds) }, dependencies);
    if (summary.status === 'passed') {
      io.log(JSON.stringify({ status: summary.status, rounds: summary.rounds.length, outputDir: summary.outputDir }));
      return 0;
    }
    io.error(safeCode(summary.error?.code, 'session_failed'));
    return summary.status === 'interrupted' ? 130 : 1;
  } catch (error) {
    io.error(safeCode(error.code, 'invalid_arguments'));
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await runCli();
}
