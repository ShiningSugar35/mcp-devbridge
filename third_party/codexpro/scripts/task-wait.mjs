// Wait for process termination AND the attached durable receipt. This module
// observes tasks only; a local timeout never cancels or replays the command.
import { performance } from 'node:perf_hooks';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

export async function waitForTask(targetClient, startResult, workspaceId, waitSeconds = 10) {
  const taskId = startResult.structuredContent?.task_id ?? startResult.structuredContent?.task?.taskId;
  if (!taskId) throw new Error('command did not return task_id');
  if (!(Number.isFinite(waitSeconds) && waitSeconds > 0)) throw new Error('task wait deadline must be positive');
  const deadline = performance.now() + Math.ceil(waitSeconds * 1000);
  let lastState = 'missing';
  for (;;) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new Error(`task wait deadline exceeded while ${lastState}`);
    let timer;
    let response;
    try {
      response = await Promise.race([
        Promise.resolve().then(() => targetClient.request('tools/call', {
          name: 'wait_task',
          arguments: {
            ...(workspaceId ? { workspace_id: workspaceId } : {}),
            task_id: taskId,
            wait_seconds: Math.max(1, Math.min(5, Math.floor(remaining / 1000)))
          }
        })),
        new Promise((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error('task wait deadline exceeded during poll')), Math.ceil(remaining));
        })
      ]);
    } finally {
      clearTimeout(timer);
    }
    if (performance.now() > deadline) throw new Error('task wait deadline exceeded during poll');
    if (response?.isError) throw new Error('wait_task returned a tool error');
    const task = response?.structuredContent?.task;
    if (!task || !task.status) throw new Error('wait_task returned no task status');
    const durable = task.durableResolutionState ?? task.durable_resolution_state;
    lastState = `${task.status}/${durable ?? 'unattached'}`;
    if (TERMINAL.has(task.status)) {
      if (durable === 'failed' || durable === 'retry_exhausted') {
        throw new Error('durable task result could not be persisted');
      }
      if (!durable || durable === 'persisted' || durable === 'unattached') return response;
    } else if (!['running', 'cancelling', 'starting'].includes(task.status)) {
      throw new Error(`unexpected task status: ${task.status}`);
    }
    const delay = Math.min(30, Math.max(0, deadline - performance.now()));
    if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay));
  }
}
