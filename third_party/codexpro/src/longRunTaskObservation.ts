import type { BashTaskManager, BashTaskSnapshot } from "./bashOps.js";
import type { Workspace } from "./guard.js";
import type { LongRunState, LongRunTaskObservation } from "./longRunOps.js";

function taskTerminal(status: BashTaskSnapshot["status"]): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

export function observeLongRunTasks(
  bashTasks: Pick<BashTaskManager, "get">,
  workspace: Workspace,
  state: LongRunState,
): LongRunTaskObservation[] {
  return state.taskIds.map((taskId) => {
    const resolution = state.taskResolutions[taskId];
    if (resolution) {
      return { taskId, status: resolution.status, detail: `durably resolved: ${resolution.evidence}` };
    }
    try {
      const task = bashTasks.get(workspace, taskId);
      if (taskTerminal(task.status) && task.durableResolutionState) {
        if (task.durableResolutionState === "failed") {
          return {
            taskId,
            status: "unknown",
            detail:
              `Task is terminal in memory but automatic durable resolution failed after ` +
              `${task.durableResolutionAttempts ?? 0} attempts; explicit terminal evidence is required.`,
          };
        }
        return {
          taskId,
          status: "running",
          detail:
            `Task is terminal in memory (${task.status}) but durable resolution is ` +
            `${task.durableResolutionState}; review/completion remains blocked until it is persisted.`,
        };
      }
      return {
        taskId,
        status: task.status,
        detail:
          task.status === "running" || task.status === "cancelling"
            ? `${task.durationMs} ms elapsed`
            : `exit=${task.exitCode}${task.signal ? ` signal=${task.signal}` : ""}`,
      };
    } catch {
      return {
        taskId,
        status: "unknown",
        detail:
          "Task manager no longer has this id; explicit terminal resolution evidence is required before completion.",
      };
    }
  });
}
