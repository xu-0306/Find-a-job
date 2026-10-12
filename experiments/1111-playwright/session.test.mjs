import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, mkdir, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { EventEmitter } from 'node:events';
import { runSession, runCli } from './session.mjs';

const passed = () => ({ status: 'passed', pages: [{ jobs: [{ id: '1' }] }, { jobs: [{ id: '2' }] }], details: [{ id: '1' }, { id: '2' }] });
async function fixture(t, overrides = {}) {
  const outputRoot = await mkdtemp(resolve(tmpdir(), '1111-session-test-'));
  t.after(() => rm(outputRoot, { recursive: true, force: true }));
  const page = {};
  const calls = [];
  const signals = new EventEmitter();
  const context = { pages: () => [page], close: async () => { calls.push('close'); } };
  const dependencies = {
    outputRoot, roundDelayMs: 0, signalSource: signals,
    launch: async (profilePath, options) => { calls.push({ phase: 'launch', profilePath, options }); return context; },
    probe: async (ctx, options) => {
      calls.push({ phase: 'round', page: ctx.pages()[0], options });
      await mkdir(options.outputDir, { recursive: true });
      return passed();
    }, ...overrides,
  };
  return { outputRoot, page, calls, signals, context, dependencies };
}
const lockExists = root => access(resolve(root, 'session-profile.lock'));

test('launches and closes once, reuses one page, and runs bounded rounds in order', async t => {
  const f = await fixture(t);
  const summary = await runSession({ keyword: '採購', area: '101600', rounds: 3 }, f.dependencies);
  assert.equal(summary.status, 'passed');
  assert.equal(f.calls.filter(call => call.phase === 'launch').length, 1);
  assert.equal(f.calls.filter(call => call === 'close').length, 1);
  const rounds = f.calls.filter(call => call.phase === 'round');
  assert.equal(rounds.length, 3);
  assert.ok(rounds.every(call => call.page === f.page && call.options.closePage === false));
  assert.deepEqual(rounds.map(call => call.options.outputDir), [1, 2, 3].map(round => resolve(summary.outputDir, `round-${round}`)));
  assert.deepEqual(f.calls[0].options, { channel: 'chrome', headless: false, viewport: null });
  assert.deepEqual(summary.events.map(event => event.phase), ['launch', 'round', 'round', 'round', 'close']);
  assert.deepEqual(summary.rounds.map(round => round.counts), Array.from({ length: 3 }, () => ({ pages: 2, details: 2, jobs: 2 })));
  assert.ok(summary.events.every(event => Number.isFinite(event.elapsedMs) && event.elapsedMs >= 0));
  const stored = JSON.parse(await readFile(resolve(summary.outputDir, 'session-summary.json'), 'utf8'));
  assert.equal(stored.status, 'passed');
  assert.equal(stored.rounds[2].reportPath, 'round-3/report.json');
  assert.equal(f.signals.listenerCount('SIGINT'), 0);
  await assert.rejects(lockExists(f.outputRoot), { code: 'ENOENT' });
});

test('new invocations use the same dedicated profile and separate reports', async t => {
  const f = await fixture(t);
  const first = await runSession({ rounds: 1 }, f.dependencies);
  const second = await runSession({ rounds: 1 }, f.dependencies);
  const launches = f.calls.filter(call => call.phase === 'launch');
  assert.equal(launches[0].profilePath, resolve(f.outputRoot, 'session-profile'));
  assert.equal(launches[1].profilePath, launches[0].profilePath);
  assert.notEqual(first.outputDir, second.outputDir);
});

test('a non-passed round stops immediately without retry or additional rounds', async t => {
  const f = await fixture(t);
  let count = 0;
  f.dependencies.probe = async () => {
    count++;
    return { status: 'blocked_or_loading', pages: [], details: [], error: { code: 'incomplete_page', message: 'secret-token' } };
  };
  const summary = await runSession({ rounds: 3 }, f.dependencies);
  assert.equal(count, 1);
  assert.equal(summary.status, 'blocked_or_loading');
  assert.equal(summary.error.code, 'incomplete_page');
  assert.equal(f.calls.at(-1), 'close');
  assert.ok(!JSON.stringify(summary).includes('secret-token'));
});

test('an existing lock is not removed or replaced and no browser is launched', async t => {
  const f = await fixture(t);
  await mkdir(resolve(f.outputRoot, 'session-profile.lock'));
  await assert.rejects(runSession({}, f.dependencies), { code: 'profile_busy' });
  assert.deepEqual(f.calls, []);
  await lockExists(f.outputRoot);
});

test('launch failure records a safe code and releases its own lock', async t => {
  const f = await fixture(t, { launch: async () => { throw new Error('token=secret browser command headers'); } });
  const summary = await runSession({}, f.dependencies);
  assert.equal(summary.status, 'failed');
  assert.equal(summary.error.code, 'launch_failed');
  assert.deepEqual(summary.stages, { launch: 'failed', round: 'not_run', close: 'not_run' });
  assert.deepEqual(summary.events.map(event => event.phase), ['launch']);
  assert.ok(!JSON.stringify(summary).includes('secret'));
  await assert.rejects(lockExists(f.outputRoot), { code: 'ENOENT' });
});

test('probe exception is safe, stops once, and still closes its context', async t => {
  const f = await fixture(t, { probe: async () => { throw new Error('private query or token'); } });
  const summary = await runSession({}, f.dependencies);
  assert.equal(summary.error.code, 'probe_failed');
  assert.equal(summary.rounds.length, 1);
  assert.equal(summary.stages.close, 'passed');
  assert.ok(!JSON.stringify(summary).includes('private'));
});

test('close failure is distinguishable and keeps the profile locked', async t => {
  const f = await fixture(t);
  let closes = 0;
  f.context.close = async () => { closes++; throw new Error('private browser endpoint'); };
  const summary = await runSession({ rounds: 1 }, f.dependencies);
  assert.equal(summary.error.code, 'close_failed');
  assert.equal(summary.stages.close, 'failed');
  assert.equal(closes, 1);
  await lockExists(f.outputRoot);
  await assert.rejects(runSession({}, f.dependencies), { code: 'profile_busy' });
  assert.ok(!JSON.stringify(summary).includes('private'));
});

test('SIGINT and SIGTERM close the owned context once and release the lock', async t => {
  for (const signal of ['SIGINT', 'SIGTERM']) {
    const f = await fixture(t);
    f.dependencies.probe = async () => {
      f.signals.emit(signal);
      f.signals.emit(signal);
      return { status: 'failed', pages: [], details: [], error: { code: 'browser_closed' } };
    };
    const summary = await runSession({}, f.dependencies);
    assert.equal(summary.status, 'interrupted');
    assert.equal(summary.error.code, 'interrupted');
    assert.equal(f.calls.filter(call => call === 'close').length, 1);
    assert.equal(summary.rounds.length, 1);
    await assert.rejects(lockExists(f.outputRoot), { code: 'ENOENT' });
  }
});

test('interruption during launch closes a newly returned context without probing', async t => {
  const f = await fixture(t);
  f.dependencies.launch = async () => { f.signals.emit('SIGINT'); return f.context; };
  const summary = await runSession({}, f.dependencies);
  assert.equal(summary.status, 'interrupted');
  assert.equal(summary.rounds.length, 0);
  assert.equal(f.calls.filter(call => call === 'close').length, 1);
  await assert.rejects(lockExists(f.outputRoot), { code: 'ENOENT' });
});

test('round bounds and unknown CLI options fail before launch with only safe output', async t => {
  const f = await fixture(t);
  for (const rounds of [0, 4, 1.5, '2']) await assert.rejects(runSession({ rounds }, f.dependencies), { code: 'invalid_arguments' });
  const output = [];
  const io = { log: message => output.push(message), error: message => output.push(message) };
  for (const args of [['--rounds', '4'], ['--profile', 'personal-token'], ['--keyword', ''], ['--area', 'private-token']]) {
    assert.equal(await runCli(args, f.dependencies, io), 1);
  }
  assert.deepEqual(output, Array(4).fill('invalid_arguments'));
  assert.deepEqual(f.calls, []);
});

test('CLI emits safe codes for browser errors and unknown probe codes', async t => {
  const f = await fixture(t);
  const output = [];
  const io = { log: message => output.push(message), error: message => output.push(message) };
  f.dependencies.launch = async () => { throw new Error('secret token headers'); };
  assert.equal(await runCli(['--rounds', '1'], f.dependencies, io), 1);
  assert.deepEqual(output, ['launch_failed']);
  f.dependencies.launch = async () => f.context;
  f.dependencies.probe = async () => ({ status: 'failed', error: { code: 'secret_token_123', message: 'secret' } });
  assert.equal(await runCli(['--rounds', '1'], f.dependencies, io), 1);
  assert.deepEqual(output, ['launch_failed', 'round_failed']);
});

test('interrupting the inter-round wait does not wait another 20 seconds', async t => {
  const f = await fixture(t, { roundDelayMs: 20000 });
  f.dependencies.probe = async () => {
    setImmediate(() => f.signals.emit('SIGINT'));
    return passed();
  };
  const started = Date.now();
  const summary = await runSession({}, f.dependencies);
  assert.equal(summary.status, 'interrupted');
  assert.equal(summary.rounds.length, 1);
  assert.ok(Date.now() - started < 2000);
});

test('a close failure during interruption retains protection and close_failed', async t => {
  const f = await fixture(t);
  f.context.close = async () => { throw new Error('secret'); };
  f.dependencies.probe = async () => {
    f.signals.emit('SIGINT');
    return passed();
  };
  const summary = await runSession({}, f.dependencies);
  assert.equal(summary.status, 'failed');
  assert.equal(summary.error.code, 'close_failed');
  const stored = JSON.parse(await readFile(resolve(summary.outputDir, 'session-summary.json'), 'utf8'));
  assert.equal(stored.error.code, 'close_failed');
  await lockExists(f.outputRoot);
});

test('SIGINT during normal close is recorded as interrupted', async t => {
  const f = await fixture(t);
  f.context.close = async () => { f.signals.emit('SIGINT'); };
  const summary = await runSession({ rounds: 1 }, f.dependencies);
  assert.equal(summary.status, 'interrupted');
  assert.equal(summary.error.code, 'interrupted');
  await assert.rejects(lockExists(f.outputRoot), { code: 'ENOENT' });
});
