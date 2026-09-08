import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { setTimeout as delay } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const built = (name) => import(pathToFileURL(path.join(root, 'dist', name)).href);
const [{ loadConfig }, { PathGuard, WorkspaceManager }, { searchWorkspace }, analysisApi] = await Promise.all([
  built('config.js'), built('guard.js'), built('searchOps.js'), built('analysis/index.js'),
]);
const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-search-deadline-'));
const originalStat = fs.stat;
const originalRead = fs.readFile;
const originalSpawn = cp.spawn;
let rgMode = false;
let commandDelayMs = 0;
let rgPid;
let rgArgs;
const fakeMatch = JSON.stringify({ type: 'match', data: {
  path: { text: path.join(tmp, 'a-first.txt') }, line_number: 1, lines: { text: 'needle\n' },
} });
cp.spawn = (name, args, options) => {
  if (name === 'where' || name === '/bin/sh') {
    return originalSpawn(process.execPath, ['-e', `setTimeout(()=>process.exit(${rgMode ? 0 : 1}), ${commandDelayMs})`], options);
  }
  if (name === 'rg') {
    rgArgs = args;
    const child = originalSpawn(process.execPath, ['-e', `console.log(${JSON.stringify(fakeMatch)}); setInterval(()=>{},1000)`], options);
    rgPid = child.pid;
    return child;
  }
  return originalSpawn(name, args, options);
};
syncBuiltinESMExports();

try {
  await fs.writeFile(path.join(tmp, 'a-first.txt'), 'needle first\n');
  await fs.writeFile(path.join(tmp, 'z-slow.txt'), 'needle last\n');
  const config = loadConfig(['--root', tmp, '--bash', 'off', '--write', 'off']);
  config.analysisEnabled = false;
  const guard = new PathGuard(config);
  const workspace = new WorkspaceManager(config).defaultWorkspace();

  // Default search policy is 30s. This observes the real timer without waiting for it.
  {
    const originalSetTimeout = globalThis.setTimeout;
    const delays = [];
    globalThis.setTimeout = function(callback, ms, ...args) {
      delays.push(ms);
      return originalSetTimeout(callback, ms, ...args);
    };
    try {
      await searchWorkspace(config, guard, workspace, { query: 'needle', root: 'a-first.txt' });
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
    assert(delays.includes(30_000), `default search deadline must be 30000ms, observed ${delays.join(',')}`);
  }

  // Structured search starts after command discovery: its build/wait budget must use
  // the OUTER deadline's remaining time, capped below 25s, not restart a full window.
  {
    analysisApi.invalidateWorkspaceAnalysis(workspace.id);
    commandDelayMs = 100;
    const originalSetTimeout = globalThis.setTimeout;
    const delays = [];
    globalThis.setTimeout = function(callback, ms, ...args) {
      delays.push(ms);
      return originalSetTimeout(callback, ms, ...args);
    };
    try {
      await searchWorkspace({ ...config, analysisEnabled: true }, guard, workspace, {
        query: 'needle', root: 'a-first.txt', intent: 'text', timeoutMs: 1_000
      });
    } finally {
      globalThis.setTimeout = originalSetTimeout;
      commandDelayMs = 0;
    }
    assert(delays.includes(1_000), `outer test deadline missing: ${delays.join(',')}`);
    const remainingBudgets = delays.filter((ms) => ms >= 300 && ms < 1_000);
    assert(remainingBudgets.length >= 2, `structured build/wait must share remaining budget, observed ${delays.join(',')}`);
    assert(!delays.includes(15_000), `search-triggered analysis must not restart the legacy15s build timer: ${delays.join(',')}`);
  }
  fs.stat = async (...args) => {
    if (path.resolve(String(args[0])) === tmp) await delay(650);
    return originalStat(...args);
  };
  const started = performance.now();
  const timed = await searchWorkspace(config, guard, workspace, { query: 'needle', timeoutMs: 100 });
  const elapsed = performance.now() - started;
  assert(elapsed < 500, `search ignored shared deadline: ${elapsed}ms`);
  assert(timed.truncated && timed.warnings?.some((w) => /deadline|budget/i.test(w)));
  assert(!timed.text.includes('No matches.'));
  fs.stat = originalStat;
  await delay(800); // Underlying uncancellable stat must finish before more probes.
  if (!process.argv.includes('--deadline-only')) {
    let unblock;
    const barrier = new Promise((resolve) => { unblock = resolve; });
    fs.readFile = async (...args) => {
      if (String(args[0]).endsWith('z-slow.txt')) await barrier;
      return originalRead(...args);
    };
    const partial = await searchWorkspace(config, guard, workspace, { query: 'needle', timeoutMs: 300 });
    assert(partial.truncated && partial.matches.some((m) => m.path === 'a-first.txt'));
    assert(!partial.matches.some((m) => m.path === 'z-slow.txt'));
    unblock();
    fs.readFile = originalRead;
    await delay(100);
    const complete = await searchWorkspace(config, guard, workspace, { query: 'needle' });
    assert.equal(complete.matches.length, 2);
    assert.equal(complete.truncated, false);
    const limited = await searchWorkspace(config, guard, workspace, { query: 'needle', maxResults: 1 });
    assert.equal(limited.matches.length, 1);
    assert.equal(limited.truncated, true);
    const scoped = await searchWorkspace(config, guard, workspace, { query: 'needle', root: 'a-first.txt' });
    assert.deepEqual(scoped.matches.map((m) => m.path), ['a-first.txt']);

    let release;
    const held = new Promise((resolve) => { release = resolve; });
    fs.stat = async (...args) => {
      if (path.resolve(String(args[0])) === tmp) await held;
      return originalStat(...args);
    };
    const pending = Array.from({ length: 8 }, () => searchWorkspace(config, guard, workspace, { query: 'needle', timeoutMs: 400 }));
    await delay(200);
    await assert.rejects(searchWorkspace(config, guard, workspace, { query: 'needle' }), /busy|capacity/i);
    const values = await Promise.all(pending);
    assert(values.every((v) => v.truncated));
    await assert.rejects(searchWorkspace(config, guard, workspace, { query: 'needle' }), /busy|capacity/i);
    release();
    fs.stat = originalStat;
    await delay(100);
    assert.equal((await searchWorkspace(config, guard, workspace, { query: 'needle' })).matches.length, 2);

    const cancel = new AbortController();
    cancel.abort();
    await assert.rejects(searchWorkspace(config, guard, workspace, { query: 'needle', signal: cancel.signal }), /abort|cancel/i);
    rgMode = true;
    const rg = await searchWorkspace(config, guard, workspace, { query: 'needle', regex: true, timeoutMs: 500 });
    assert(rg.truncated && rg.used === 'ripgrep');
    assert.equal(rg.matches.length, 1);
    assert(rgArgs.includes('-e') && rgArgs.includes('needle'));
    await delay(200);
    assert.throws(() => process.kill(rgPid, 0), /ESRCH|not found/i);
  }
  console.log('Search deadline, partial results, capacity, cancellation and child cleanup smoke passed.');
} finally {
  fs.stat = originalStat;
  fs.readFile = originalRead;
  cp.spawn = originalSpawn;
  syncBuiltinESMExports();
  if (rgPid) { try { process.kill(rgPid); } catch {} }
  await fs.rm(tmp, { recursive: true, force: true });
}
