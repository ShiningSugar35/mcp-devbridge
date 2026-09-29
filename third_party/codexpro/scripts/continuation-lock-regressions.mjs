import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { LongRunStore } from '../dist/longRunOps.js';
import { PathGuard } from '../dist/guard.js';

async function fixture(fn) {
  const base = path.resolve('.tmp/continuation-lock-regressions');
  await fs.mkdir(base, { recursive: true });
  const tmp = await fs.mkdtemp(path.join(base, 'case-'));
  const root = path.join(tmp, 'workspace');
  await fs.mkdir(root);
  const workspace = { id: 'ws-lock-regression', root: await fs.realpath(root), openedAt: new Date().toISOString() };
  const store = new LongRunStore('.ai-bridge', new PathGuard({ blockedGlobs: [] }));
  try {
    const run = await store.start(workspace, { title: 'Lock test', objective: 'No duplicate writer', steps: [{ title: 'one', acceptance_criteria: ['verified'] }] });
    const lockDir = path.join(root, '.ai-bridge/long-runs/.locks');
    const lockPath = path.join(lockDir, run.runId + '.json.lock');
    const target = path.join(root, '.ai-bridge/long-runs', run.runId + '.json');
    await fn({ tmp, workspace, store, run, lockDir, lockPath, target });
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
}

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

async function bounded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('fixture coordination deadline')), 2500);
    })]);
  } finally { clearTimeout(timer); }
}

test('a lock-directory junction cannot redirect coordination writes outside the workspace', async () => fixture(async ({ tmp, workspace, store, run, lockDir }) => {
  const outside = path.join(tmp, 'outside-workspace');
  await fs.mkdir(outside);
  await fs.symlink(outside, lockDir, process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(store.update(workspace, run.runId, { checkpoint: 'must be rejected' }), /outside|symbolic|symlink|junction|escape/i);
  assert.deepEqual(await fs.readdir(outside), []);
}));

test('failed lock initialization must not poison later attempts', async () => fixture(async ({ workspace, store, run, lockPath }) => {
  const originalOpen = fs.open;
  fs.open = async (...args) => {
    const handle = await originalOpen(...args);
    if (String(args[0]) === lockPath && args[1] === 'wx') {
      handle.writeFile = async () => { throw Object.assign(new Error('injected lock write failure'), { code: 'ENOSPC' }); };
    }
    return handle;
  };
  try {
    await assert.rejects(store.update(workspace, run.runId, { checkpoint: 'write fails' }), /injected lock write failure/);
  } finally { fs.open = originalOpen; }
  await assert.rejects(fs.stat(lockPath), error => error.code === 'ENOENT');
  await store.update(workspace, run.runId, { checkpoint: 'next attempt works' });
}));

test('two stale reapers cannot remove the replacement lock and both write revision zero', async () => fixture(async ({ workspace, store, run, lockDir, lockPath, target }) => {
  await fs.mkdir(lockDir, { recursive: true });
  await fs.writeFile(lockPath, JSON.stringify({ pid: 2147483647, token: '[REDACTED_SECRET]', createdAt: '2026-01-01T00:00:00Z' }));
  const old = new Date(Date.now() - 120000);
  await fs.utimes(lockPath, old, old);
  const second = new LongRunStore('.ai-bridge', new PathGuard({ blockedGlobs: [] }));
  const seenOld = deferred(), releaseOld = deferred(), acquiredSecond = deferred(), releaseSecond = deferred();
  const readFile = fs.readFile;
  let firstStaleRead = true, firstTargetRead = true;
  fs.readFile = async (...args) => {
    const result = await readFile(...args);
    if (String(args[0]) === lockPath && firstStaleRead && String(result).includes('[REDACTED_SECRET]')) {
      firstStaleRead = false;
      seenOld.resolve();
      await bounded(releaseOld.promise);
    } else if (String(args[0]) === target && firstTargetRead) {
      firstTargetRead = false;
      acquiredSecond.resolve();
      await bounded(releaseSecond.promise);
    }
    return result;
  };
  let a, b;
  try {
    a = store.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 });
    a.catch(() => {});
    await bounded(seenOld.promise);
    b = second.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 });
    b.catch(() => {});
    await bounded(acquiredSecond.promise);
    releaseOld.resolve();
    const first = await bounded(Promise.allSettled([a]));
    releaseSecond.resolve();
    await bounded(b);
    assert.equal(first[0].status, 'rejected', 'the delayed reaper must not evict a live replacement owner');
    assert.equal((await store.read(workspace, run.runId)).continuation.revision, 1);
  } finally {
    releaseOld.resolve(); releaseSecond.resolve();
    await Promise.allSettled([a, b].filter(Boolean));
    fs.readFile = readFile;
  }
}));
