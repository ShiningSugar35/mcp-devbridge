import assert from 'node:assert/strict';
import { waitForTask } from './task-wait.mjs';

const start = { structuredContent: { task_id: 'fixture-task' } };
function fixture(states) {
  let calls = 0;
  return {
    get calls() { return calls; },
    async request(method, body) {
      assert.equal(method, 'tools/call');
      assert.equal(body.name, 'wait_task');
      assert.equal(body.arguments.task_id, 'fixture-task');
      assert(Number.isInteger(body.arguments.wait_seconds) && body.arguments.wait_seconds >= 1);
      const task = states[Math.min(calls++, states.length - 1)];
      return { structuredContent: { task } };
    }
  };
}

const durable = fixture([
  { status: 'completed', durableResolutionState: 'persisting' },
  { status: 'completed', durableResolutionState: 'retrying' },
  { status: 'completed', durableResolutionState: 'persisted' },
]);
const settled = await waitForTask(durable, start, 'workspace', 1);
assert.equal(settled.structuredContent.task.durableResolutionState, 'persisted');
assert.equal(durable.calls, 3);

const ordinary = fixture([{ status: 'running' }, { status: 'completed', exitCode: 0 }]);
assert.equal((await waitForTask(ordinary, start, undefined, 1)).structuredContent.task.status, 'completed');
assert.equal(ordinary.calls, 2);

const failed = fixture([{ status: 'completed', durableResolutionState: 'failed' }]);
await assert.rejects(waitForTask(failed, start, undefined, 1), /durable/i);
const stalled = fixture([{ status: 'completed', durableResolutionState: 'persisting' }]);
await assert.rejects(waitForTask(stalled, start, undefined, 0.03), /deadline|timed out/i);
await assert.rejects(waitForTask(ordinary, { structuredContent: {} }, undefined, 1), /task_id/);

const cancelled = fixture([{ status: 'cancelled', durableResolutionState: 'persisted' }]);
assert.equal((await waitForTask(cancelled, start, undefined, 1)).structuredContent.task.status, 'cancelled');
const slowResponse = {
  async request() {
    await new Promise(resolve => setTimeout(resolve, 80));
    return { structuredContent: { task: { status: 'completed', durableResolutionState: 'persisted' } } };
  }
};
await assert.rejects(waitForTask(slowResponse, start, undefined, 0.02), /deadline|timed out/i);
const toolError = { async request() { return { isError: true }; } };
await assert.rejects(waitForTask(toolError, start, undefined, 1), /tool error/i);
console.log(JSON.stringify({ suite: 'task-wait-durability', passed: 8, failed: 0 }));
