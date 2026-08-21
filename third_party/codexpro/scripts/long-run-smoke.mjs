import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LongRunStore } from '../dist/longRunOps.js';
import { PathGuard } from '../dist/guard.js';

function guard() {
  return new PathGuard({ blockedGlobs: ['**/.git/**', '**/.env', '**/.env.*'] });
}

async function expectReject(fn, pattern) {
  let error;
  try {
    await fn();
  } catch (caught) {
    error = caught;
  }
  if (!error) throw new Error('operation unexpectedly succeeded');
  if (pattern && !pattern.test(String(error?.message ?? error))) {
    throw new Error(`unexpected error: ${String(error?.message ?? error)}`);
  }
}

const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-long-run-'));
try {
  const workspace = { id: 'ws-long-run-smoke', root: await fs.realpath(tmp), openedAt: new Date().toISOString() };
  const store = new LongRunStore('.ai-bridge', guard());
  const started = await store.start(workspace, {
    title: 'durable release smoke',
    objective: 'prove plan, execution, rework, review and durable completion gates',
    steps: [
      { title: 'implement', acceptance_criteria: ['implementation evidence exists'] },
      { title: 'verify', acceptance_criteria: ['verification evidence exists'] }
    ],
    acceptanceCriteria: ['final review passes against current work revision']
  });

  assert.match(started.runId, /^lr_/);
  assert.equal(started.status, 'working');
  await expectReject(
    () => store.update(workspace, started.runId, { stepId: 's1', stepStatus: 'done' }),
    /without evidence/
  );

  await Promise.all([
    store.update(workspace, started.runId, {
      stepId: 's1',
      stepStatus: 'done',
      evidence: ['implementation diff reviewed'],
      checkpoint: 'implementation finished'
    }),
    store.update(workspace, started.runId, {
      stepId: 's2',
      stepStatus: 'done',
      evidence: ['targeted smoke passed'],
      checkpoint: 'verification finished'
    })
  ]);
  let state = await store.read(workspace, started.runId);
  assert.deepEqual(state.steps.map((step) => step.status), ['done', 'done']);
  assert.equal(state.status, 'reviewing');

  state = await store.review(workspace, started.runId, {
    verdict: 'fail',
    summary: 'verification evidence needs a stronger regression check',
    failedStepIds: ['s2'],
    failedCriteria: ['verification evidence exists'],
    requiredRework: ['rerun the full smoke suite'],
    evidence: ['reviewed current plan and targeted smoke output']
  });
  assert.equal(state.status, 'rework');
  assert.equal(state.steps[1].status, 'pending');

  state = await store.update(workspace, started.runId, {
    stepId: 's2',
    stepStatus: 'done',
    evidence: ['full smoke suite passed'],
    checkpoint: 'rework completed'
  });
  state = await store.review(workspace, started.runId, {
    verdict: 'pass',
    summary: 'all persisted criteria are satisfied',
    evidence: ['implementation diff reviewed', 'full smoke suite passed']
  });
  assert.equal(state.reviews.at(-1)?.verdict, 'pass');

  state = await store.update(workspace, started.runId, {
    stepId: 's1',
    note: 'post-review implementation note changed the work revision'
  });
  await expectReject(
    () => store.complete(workspace, started.runId, 'should be stale', []),
    /PASS review is missing or stale/
  );
  state = await store.review(workspace, started.runId, {
    verdict: 'pass',
    summary: 'review refreshed after the work revision changed',
    evidence: ['review repeated against the current revision']
  });

  await expectReject(
    () => store.complete(workspace, started.runId, 'still running', [{ taskId: 'task-live', status: 'running' }]),
    /still running/
  );

  state = await store.update(workspace, started.runId, {
    taskId: 'task-lost',
    checkpoint: 'attached task before simulated MCP restart'
  });
  await expectReject(
    () => store.complete(workspace, started.runId, 'unknown task', [{ taskId: 'task-lost', status: 'unknown' }]),
    /unknown after reconnect\/restart/
  );
  state = await store.update(workspace, started.runId, {
    resolveTaskId: 'task-lost',
    resolveTaskStatus: 'completed',
    resolveTaskEvidence: 'external job record reports exit code 0'
  });
  state = await store.review(workspace, started.runId, {
    verdict: 'pass',
    summary: 'explicit terminal task resolution is evidenced and current',
    evidence: ['external job record reports exit code 0']
  });

  const restartedStore = new LongRunStore('.ai-bridge', guard());
  const recovered = await restartedStore.read(workspace, started.runId);
  assert.equal(recovered.runId, started.runId);
  assert.equal(recovered.taskResolutions['task-lost']?.status, 'completed');
  const completed = await restartedStore.complete(
    workspace,
    started.runId,
    'durable quality gate passed after restart recovery',
    [{ taskId: 'task-lost', status: 'unknown' }]
  );
  assert.equal(completed.status, 'completed');

  const fakeSecret = 'sk-' + 'abcdefghijklmnopqrstuvwxyz1234567890';
  await expectReject(
    () => store.start(workspace, {
      title: 'secret reject',
      objective: `contains ${fakeSecret}`,
      steps: [{ title: 'x', acceptance_criteria: ['y'] }]
    }),
    /appears to contain a secret/
  );

  const corruptDir = path.join(tmp, '.ai-bridge', 'long-runs');
  await fs.writeFile(path.join(corruptDir, 'lr_corrupt_state.json'), '{not-json', 'utf8');
  const listed = await restartedStore.list(workspace);
  assert.ok(listed.some((item) => item.runId === started.runId));
  assert.ok(!listed.some((item) => item.runId === 'lr_corrupt_state'));
  await expectReject(() => restartedStore.read(workspace, 'lr_corrupt_state'), /invalid JSON/);

  const escapeRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-long-run-escape-'));
  const escapeOutside = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-long-run-outside-'));
  try {
    const workspaceEscape = { id: 'ws-long-run-escape', root: await fs.realpath(escapeRoot), openedAt: new Date().toISOString() };
    const link = path.join(escapeRoot, '.ai-bridge');
    try {
      await fs.symlink(escapeOutside, link, process.platform === 'win32' ? 'junction' : 'dir');
      await expectReject(
        () => new LongRunStore('.ai-bridge', guard()).start(workspaceEscape, {
          title: 'escape',
          objective: 'must not write through a context symlink',
          steps: [{ title: 'x', acceptance_criteria: ['stay in workspace'] }]
        }),
        /outside|symlink|escapes/i
      );
    } catch (error) {
      if (process.platform !== 'win32' || error?.code !== 'EPERM') throw error;
    }
  } finally {
    await fs.rm(escapeRoot, { recursive: true, force: true });
    await fs.rm(escapeOutside, { recursive: true, force: true });
  }

  console.log('long-run-smoke: ok');
} finally {
  await fs.rm(tmp, { recursive: true, force: true });
}
