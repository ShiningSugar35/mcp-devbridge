import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { LongRunStore } from '../dist/longRunOps.js';
import { PathGuard } from '../dist/guard.js';

async function fixture(fn) {
  const base = path.resolve(process.cwd(), '.tmp', 'continuation-smoke');
  await fs.mkdir(base, { recursive: true });
  const tmp = await fs.mkdtemp(path.join(base, 'case-'));
  try {
    const workspace = { id: 'ws-continuation-test', root: await fs.realpath(tmp), openedAt: new Date().toISOString() };
    const store = new LongRunStore('.ai-bridge', new PathGuard({ blockedGlobs: ['**/.git/**', '**/.env'] }));
    const run = await store.start(workspace, {
      title: 'scheduled continuation fixture', objective: 'resume only one durable run',
      steps: [{ title: 'implementation', acceptance_criteria: ['fixture passes'] }]
    });
    await fn({ store, workspace, run });
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
}

test('failed replacement never removes the last valid run', async () => fixture(async ({ store, workspace, run }) => {
  const target = path.join(workspace.root, '.ai-bridge', 'long-runs', `${run.runId}.json`);
  const before = await fs.readFile(target, 'utf8');
  const rename = fs.rename;
  const rm = fs.rm;
  let targetDeletes = 0;
  fs.rename = async (src, dst) => {
    if (dst === target) throw Object.assign(new Error('injected sharing violation'), { code: 'EPERM' });
    return rename(src, dst);
  };
  fs.rm = async (file, opts) => {
    if (file === target) targetDeletes += 1;
    return rm(file, opts);
  };
  try {
    await assert.rejects(store.update(workspace, run.runId, { checkpoint: 'must not replace on failure' }));
  } finally { fs.rename = rename; fs.rm = rm; }
  assert.equal(targetDeletes, 0, 'canonical state must never be deleted as a rename fallback');
  assert.equal(await fs.readFile(target, 'utf8'), before);
  assert.deepEqual((await fs.readdir(path.dirname(target))).filter(p => p.endsWith('.tmp')), []);
}));

test('Scheduled binding and bounded windows survive restart without changing work revision', async () => fixture(async ({ store, workspace, run }) => {
  assert.equal(typeof store.updateContinuation, 'function');
  const update = async (input, observations = [], now = new Date('2026-09-29T03:00:00Z')) =>
    store.updateContinuation(workspace, run.runId, input, observations, now);
  let state = await update({ operation: 'request_schedule', expected_revision: 0 });
  const requestId = state.continuation.scheduler.requestId;
  assert.equal(state.schemaVersion, 2, 'old writers must reject new control state instead of stripping it');
  assert.equal(state.continuation.scheduler.status, 'requested');
  const duplicate = await update({ operation: 'request_schedule', expected_revision: 1 });
  assert.equal(duplicate.continuation.revision, 1);
  state = await update({ operation: 'bind_schedule', expected_revision: 1, request_id: requestId, automation_id: 'fixture-schedule-0001', cadence_minutes: 60 });
  assert.equal(state.continuation.scheduler.status, 'bound');
  await assert.rejects(update({ operation: 'bind_schedule', expected_revision: 2, request_id: requestId, automation_id: 'fixture-schedule-0002', cadence_minutes: 60 }), /already bound|conflict/i);
  state = await update({ operation: 'open_window', expected_revision: 2, invocation_id: 'fixture-window-001', source: 'scheduled' });
  const deadline = state.continuation.window.deadline;
  assert.equal(deadline, '2026-09-29T03:35:00.000Z');
  state = await update({ operation: 'open_window', expected_revision: 3, invocation_id: 'fixture-window-001', source: 'scheduled' }, [], new Date('2026-09-29T03:10:00Z'));
  assert.equal(state.continuation.revision, 3);
  assert.equal(state.continuation.window.deadline, deadline);
  await assert.rejects(update({ operation: 'open_window', expected_revision: 3, invocation_id: 'fixture-window-002', source: 'scheduled' }), /active window/i);
  await assert.rejects(update({ operation: 'yield_window', expected_revision: 1, invocation_id: 'fixture-window-001', next_checkpoint: 's1 tests' }), /revision/i);
  state = await update({ operation: 'yield_window', expected_revision: 3, invocation_id: 'fixture-window-001', next_checkpoint: 's1 targeted regression' }, [], new Date('2026-09-29T03:34:00Z'));
  assert.equal(state.continuation.window.status, 'yielded');
  assert.equal(state.workRevision, run.workRevision);
  const restarted = new LongRunStore('.ai-bridge', new PathGuard({ blockedGlobs: [] }));
  assert.deepEqual((await restarted.read(workspace, run.runId)).continuation, state.continuation);
}));

test('stale admission needs real task observations and never fabricates old end', async () => fixture(async ({ store, workspace, run }) => {
  const update = (input, observations = [], now = new Date('2026-09-29T03:00:00Z')) => store.updateContinuation(workspace, run.runId, input, observations, now);
  await update({ operation: 'open_window', expected_revision: 0, invocation_id: 'fixture-window-001', source: 'chat' });
  const later = new Date('2026-09-29T04:00:00Z');
  await assert.rejects(update({ operation: 'open_window', expected_revision: 1, invocation_id: 'fixture-window-002', source: 'scheduled' }, [{ taskId: 'active-task', status: 'running' }], later), /task/i);
  const state = await update({ operation: 'open_window', expected_revision: 1, invocation_id: 'fixture-window-002', source: 'chat' }, [{ taskId: 'old-task', status: 'completed' }], later);
  assert.equal(state.continuation.history[0].status, 'abandoned');
  assert.equal(state.continuation.history[0].endedAt, undefined);
  assert.equal(state.continuation.window.sequence, 2);
}));

test('independent stores serialize the same run revision through a filesystem lock', async () => fixture(async ({ store, workspace, run }) => {
  const secondStore = new LongRunStore('.ai-bridge', new PathGuard({ blockedGlobs: [] }));
  const target = path.join(workspace.root, '.ai-bridge', 'long-runs', `${run.runId}.json`);
  const originalReadFile = fs.readFile;
  let releaseGate;
  const gate = new Promise((resolve) => { releaseGate = resolve; });
  const timer = setTimeout(() => releaseGate(), 50);
  fs.readFile = async (...args) => {
    const result = await originalReadFile(...args);
    if (String(args[0]) === target) await gate;
    return result;
  };
  try {
    const results = await Promise.allSettled([
      store.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 }),
      secondStore.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 })
    ]);
    assert.equal(results.filter((item) => item.status === 'fulfilled').length, 1);
    assert.equal(results.filter((item) => item.status === 'rejected').length, 1);
    const state = await store.read(workspace, run.runId);
    assert.equal(state.continuation.revision, 1);
    assert.equal(state.continuation.scheduler.status, 'requested');
  } finally {
    clearTimeout(timer);
    releaseGate();
    fs.readFile = originalReadFile;
  }
}));

test('stale dead-process lock is quarantined before reuse', async () => fixture(async ({ store, workspace, run }) => {
  const lockDir = path.join(workspace.root, '.ai-bridge', 'long-runs', '.locks');
  await fs.mkdir(lockDir, { recursive: true });
  const lockPath = path.join(lockDir, `${run.runId}.json.lock`);
  await fs.writeFile(lockPath, JSON.stringify({ pid: 2147483647, createdAt: '2026-09-29T00:00:00.000Z' }), 'utf8');
  const staleAt = new Date(Date.now() - 120_000);
  await fs.utimes(lockPath, staleAt, staleAt);
  const state = await store.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 });
  assert.equal(state.continuation.revision, 1);
  assert.equal(state.continuation.scheduler.status, 'requested');
  await assert.rejects(fs.stat(lockPath), (error) => error?.code === 'ENOENT');
  assert.deepEqual((await fs.readdir(lockDir)).filter((name) => name.endsWith('.stale')), []);
}));

test('pause disables and resume re-enables the same bound scheduler', async () => fixture(async ({ store, workspace, run }) => {
  let state = await store.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 });
  const requestId = state.continuation.scheduler.requestId;
  state = await store.updateContinuation(workspace, run.runId, { operation: 'bind_schedule', expected_revision: 1, request_id: requestId, automation_id: 'fixture-schedule-0001', cadence_minutes: 60 });
  state = await store.updateContinuation(workspace, run.runId, { operation: 'pause', expected_revision: 2 });
  assert.equal((await store.continuationStatus(workspace, run.runId)).host_action_required, 'disable_schedule');
  state = await store.updateContinuation(workspace, run.runId, { operation: 'scheduler_disabled', expected_revision: 3, automation_id: 'fixture-schedule-0001' });
  assert.equal(state.continuation.scheduler.status, 'disabled');
  state = await store.updateContinuation(workspace, run.runId, { operation: 'resume', expected_revision: 4 });
  const resumed = await store.continuationStatus(workspace, run.runId);
  assert.equal(resumed.host_action_required, 'enable_schedule');
  assert.equal(resumed.scheduler.automationId, 'fixture-schedule-0001');
  await assert.rejects(
    store.updateContinuation(workspace, run.runId, { operation: 'open_window', expected_revision: 5, invocation_id: 'fixture-window-003', source: 'scheduled' }),
    /bound scheduler/i
  );
  state = await store.updateContinuation(workspace, run.runId, { operation: 'scheduler_enabled', expected_revision: 5, automation_id: 'fixture-schedule-0001' });
  assert.equal(state.continuation.scheduler.status, 'bound');
  assert.equal(state.continuation.scheduler.automationId, 'fixture-schedule-0001');
}));

test('completed run can acknowledge scheduler disable, never reopen development', async () => fixture(async ({ store, workspace, run }) => {
  let state = await store.updateContinuation(workspace, run.runId, { operation: 'request_schedule', expected_revision: 0 });
  state = await store.updateContinuation(workspace, run.runId, { operation: 'bind_schedule', expected_revision: 1, request_id: state.continuation.scheduler.requestId, automation_id: 'fixture-schedule-0001', cadence_minutes: 60 });
  await store.update(workspace, run.runId, { stepId: 's1', stepStatus: 'done', evidence: ['fixture passed'] });
  await store.review(workspace, run.runId, { verdict: 'pass', summary: 'fixture review', evidence: ['fixture passed'] });
  await store.complete(workspace, run.runId, 'fixture complete', []);
  await assert.rejects(store.updateContinuation(workspace, run.runId, { operation: 'open_window', expected_revision: 2, invocation_id: 'fixture-window-001', source: 'chat' }), /terminal/i);
  state = await store.updateContinuation(workspace, run.runId, { operation: 'scheduler_disabled', expected_revision: 2, automation_id: 'fixture-schedule-0001' });
  assert.equal(state.status, 'completed');
  assert.equal(state.continuation.scheduler.status, 'disabled');
}));
