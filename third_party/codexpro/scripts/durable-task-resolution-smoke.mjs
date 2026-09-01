import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { BashTaskManager } from "../dist/bashOps.js";
import { loadConfig } from "../dist/config.js";
import { PathGuard } from "../dist/guard.js";
import { LongRunStore } from "../dist/longRunOps.js";

process.env.CODEXPRO_BASH_MODE = "full";
process.env.CODEXPRO_ALLOW_NO_HTTP_TOKEN = "1";

const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "codexpro-durable-task-resolution-"));
const workspace = {
  id: "ws-durable-task-resolution-smoke",
  root: await fs.realpath(tmp),
  openedAt: new Date().toISOString(),
};
const config = loadConfig(["--root", workspace.root, "--bash", "full"]);
const guard = new PathGuard(config);
const store = new LongRunStore(".ai-bridge", guard);
const tasks = new BashTaskManager(undefined, 4);
const startedTasks = [];
const callbackCounts = new Map();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function removeTreeBestEffort(target) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      await fs.rm(target, { recursive: true, force: true });
      return;
    } catch (error) {
      if (!["EBUSY", "EPERM", "ENOTEMPTY"].includes(error?.code) || attempt === 9) return;
      await sleep(100 * (attempt + 1));
    }
  }
}

async function waitForResolution(runId, taskId, expectedStatus, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await store.read(workspace, runId);
    const resolution = state.taskResolutions[taskId];
    if (resolution) {
      assert.equal(resolution.status, expectedStatus);
      assert.match(resolution.evidence, /terminal receipt/i);
      return resolution;
    }
    await sleep(25);
  }
  throw new Error(`timed out waiting for durable resolution of ${taskId}`);
}

async function waitForTaskDurableState(manager, taskId, expectedState, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const snapshot = manager.get(workspace, taskId);
    if (snapshot.durableResolutionState === expectedState) return snapshot;
    await sleep(25);
  }
  throw new Error(`timed out waiting for durable callback state ${expectedState} on ${taskId}`);
}

function terminalResolver(runId) {
  return async (snapshot) => {
    callbackCounts.set(snapshot.taskId, (callbackCounts.get(snapshot.taskId) ?? 0) + 1);
    assert.ok(["completed", "failed", "cancelled"].includes(snapshot.status));
    const resolutionUpdate = {
      resolveTaskId: snapshot.taskId,
      resolveTaskStatus: snapshot.status,
      resolveTaskEvidence:
        `BashTaskManager terminal receipt: status=${snapshot.status}; ` +
        `exitCode=${snapshot.exitCode ?? "null"}; signal=${snapshot.signal ?? "null"}; ` +
        `finishedAt=${snapshot.finishedAt ?? "unknown"}.`,
    };
    await store.update(workspace, runId, resolutionUpdate);
    await store.update(workspace, runId, resolutionUpdate);
  };
}

async function attachAndStart(runId, taskId, command) {
  await store.update(workspace, runId, {
    taskId,
    checkpoint: `Reserved durable background task ${taskId} before process start.`,
  });
  const snapshot = tasks.start(config, guard, workspace, command, {
    taskId,
    onTerminal: terminalResolver(runId),
  });
  startedTasks.push(snapshot.taskId);
  assert.equal(snapshot.taskId, taskId, "the pre-attached durable task id must be used by the process manager");
  return snapshot;
}

try {
  const run = await store.start(workspace, {
    title: "durable terminal callback smoke",
    objective: "prove background task terminal state is persisted without a wait_task observation",
    steps: [{ title: "exercise task terminal states", acceptance_criteria: ["all task terminal states are durable"] }],
    acceptanceCriteria: ["completed failed and cancelled tasks are recovered after restart"],
  });

  const completedId = "task-durable-completed";
  await attachAndStart(run.runId, completedId, 'node -e "setTimeout(() => process.exit(0), 50)"');
  await waitForResolution(run.runId, completedId, "completed");

  const failedId = "task-durable-failed";
  await attachAndStart(run.runId, failedId, 'node -e "setTimeout(() => process.exit(7), 50)"');
  await waitForResolution(run.runId, failedId, "failed");

  const cancelledId = "task-durable-cancelled";
  await attachAndStart(run.runId, cancelledId, 'node -e "setTimeout(() => process.exit(0), 30000)"');
  tasks.cancel(workspace, cancelledId);
  await waitForResolution(run.runId, cancelledId, "cancelled");

  // Observation calls after terminal completion must not append duplicate durable resolutions.
  tasks.get(workspace, completedId);
  await tasks.wait(workspace, completedId, 100);
  tasks.list(workspace);
  await sleep(100);
  assert.equal(callbackCounts.get(completedId), 1);
  assert.equal(callbackCounts.get(failedId), 1);
  assert.equal(callbackCounts.get(cancelledId), 1);

  if (process.platform === "win32") {
    for (let index = 0; index < 5; index += 1) {
      const marker = path.join(tmp, `cancel-child-ready-${index}.txt`);
      const markerBase64 = Buffer.from(marker, "utf8").toString("base64");
      const taskId = `task-cancel-tree-race-${index}`;
      const started = tasks.start(
        config,
        guard,
        workspace,
        `node -e "require('fs').writeFileSync(Buffer.from('${markerBase64}','base64').toString(),'ready');setTimeout(() => process.exit(0), 30000)"`,
        { taskId },
      );
      startedTasks.push(started.taskId);
      const markerDeadline = Date.now() + 5_000;
      while (Date.now() < markerDeadline) {
        try {
          await fs.stat(marker);
          break;
        } catch {
          await sleep(25);
        }
      }
      await fs.stat(marker);
      tasks.cancel(workspace, taskId);
      const cancelled = await tasks.wait(workspace, taskId, 10_000);
      assert.equal(cancelled.status, "cancelled", `tree cancellation iteration ${index} did not terminate`);
      assert.ok(cancelled.durationMs < 10_000, `tree cancellation iteration ${index} exceeded its bound`);
    }
  }

  const retryTasks = new BashTaskManager(undefined, 2);
  let transientAttempts = 0;
  const transientId = "task-durable-transient-retry";
  retryTasks.start(config, guard, workspace, 'node -e "process.exit(0)"', {
    taskId: transientId,
    onTerminal: async () => {
      transientAttempts += 1;
      if (transientAttempts < 3) throw new Error("transient durable store failure");
    },
  });
  const transient = await waitForTaskDurableState(retryTasks, transientId, "persisted");
  assert.equal(transientAttempts, 3);
  assert.equal(transient.durableResolutionAttempts, 3);
  assert.equal(transient.durableResolutionError, undefined);

  const exhaustedTasks = new BashTaskManager(undefined, 2);
  let exhaustedAttempts = 0;
  const exhaustedId = "task-durable-retry-exhausted";
  exhaustedTasks.start(config, guard, workspace, 'node -e "process.exit(0)"', {
    taskId: exhaustedId,
    onTerminal: async () => {
      exhaustedAttempts += 1;
      throw new Error("persistent durable store failure");
    },
  });
  const exhausted = await waitForTaskDurableState(exhaustedTasks, exhaustedId, "failed");
  assert.equal(exhaustedAttempts, 5);
  assert.equal(exhausted.durableResolutionAttempts, 5);
  assert.match(exhausted.durableResolutionError ?? "", /persistent durable store failure/i);
  await sleep(250);
  assert.equal(exhaustedAttempts, 5, "terminal persistence retries must remain bounded after exhaustion");

  const restartedStore = new LongRunStore(".ai-bridge", guard);
  const recovered = await restartedStore.read(workspace, run.runId);
  assert.equal(recovered.taskResolutions[completedId]?.status, "completed");
  assert.equal(recovered.taskResolutions[failedId]?.status, "failed");
  assert.equal(recovered.taskResolutions[cancelledId]?.status, "cancelled");
  await assert.rejects(
    store.update(workspace, run.runId, {
      resolveTaskId: completedId,
      resolveTaskStatus: "failed",
      resolveTaskEvidence: "conflicting terminal evidence must not overwrite the first receipt",
    }),
    /conflicting terminal resolution/i,
  );

  console.log("durable-task-resolution-smoke: ok");
} finally {
  for (const taskId of startedTasks) {
    try {
      const task = tasks.get(workspace, taskId);
      if (task.status === "running" || task.status === "cancelling") tasks.cancel(workspace, taskId);
      await tasks.wait(workspace, taskId, 2_000);
    } catch {
      // Best-effort cleanup for the intended red baseline.
    }
  }
  await removeTreeBestEffort(tmp);
}
