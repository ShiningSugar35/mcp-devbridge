import assert from "node:assert/strict";

import { LongRunStore } from "../dist/longRunOps.js";
import { observeLongRunTasks } from "../dist/longRunTaskObservation.js";

const now = "2026-08-31T00:00:00.000Z";
const workspace = { id: "ws_observation", root: "D:\\observation" };
const taskId = "task-observation";
const baseState = {
  schemaVersion: 1,
  runId: "lr_observation",
  workspaceId: workspace.id,
  workspaceRoot: workspace.root,
  title: "observation gate",
  objective: "prove terminal-in-memory is not durable completion",
  overallAcceptanceCriteria: [],
  status: "reviewing",
  createdAt: now,
  updatedAt: now,
  workRevision: 1,
  planRevision: 1,
  reviewRound: 1,
  steps: [
    {
      id: "s1",
      title: "done",
      acceptanceCriteria: ["done"],
      status: "done",
      evidence: ["evidence"],
      notes: [],
      updatedAt: now,
    },
  ],
  checkpoints: [],
  taskIds: [taskId],
  taskResolutions: {},
  reviews: [
    {
      round: 1,
      verdict: "pass",
      summary: "pass",
      failedStepIds: [],
      failedCriteria: [],
      requiredRework: [],
      evidence: ["review evidence"],
      workRevision: 1,
      reviewedAt: now,
    },
  ],
};

const terminalSnapshot = {
  taskId,
  workspaceId: workspace.id,
  command: "redacted",
  cwd: ".",
  status: "completed",
  pid: 1,
  startedAt: now,
  lastObservedAt: now,
  orchestrationStale: false,
  orchestrationStaleAfterMs: 600_000,
  finishedAt: now,
  durationMs: 1,
  exitCode: 0,
  signal: null,
  stdout: "",
  stderr: "",
  truncated: false,
  durableResolutionAttempts: 1,
};

const store = new LongRunStore(".ai-bridge", {});

for (const durableResolutionState of ["awaiting_terminal", "persisting", "retrying", "persisted"]) {
  const observations = observeLongRunTasks(
    { get: () => ({ ...terminalSnapshot, durableResolutionState }) },
    workspace,
    structuredClone(baseState),
  );
  assert.equal(observations[0]?.status, "running");
  assert.match(observations[0]?.detail ?? "", /durable resolution/i);
  assert.ok(store.completionBlockers(baseState, observations).some((item) => item.includes(taskId)));
}

const failedObservations = observeLongRunTasks(
  { get: () => ({ ...terminalSnapshot, durableResolutionState: "failed", durableResolutionAttempts: 5 }) },
  workspace,
  structuredClone(baseState),
);
assert.equal(failedObservations[0]?.status, "unknown");
assert.ok(store.completionBlockers(baseState, failedObservations).some((item) => /unknown/i.test(item)));

const resolvedState = structuredClone(baseState);
resolvedState.taskResolutions[taskId] = {
  status: "completed",
  evidence: "automatic terminal receipt",
  resolvedAt: now,
};
const resolvedObservations = observeLongRunTasks(
  { get: () => { throw new Error("in-memory task intentionally absent after restart"); } },
  workspace,
  resolvedState,
);
assert.equal(resolvedObservations[0]?.status, "completed");
assert.deepEqual(store.completionBlockers(resolvedState, resolvedObservations), []);

const legacyObservations = observeLongRunTasks(
  { get: () => ({ ...terminalSnapshot, durableResolutionState: undefined, durableResolutionAttempts: undefined }) },
  workspace,
  structuredClone(baseState),
);
assert.equal(legacyObservations[0]?.status, "completed");
assert.deepEqual(store.completionBlockers(baseState, legacyObservations), []);

console.log("durable-task-observation-smoke: ok");
