import { createHash } from "node:crypto";
import { z } from "zod";
import { CodexProError } from "./guard.js";
import type { LongRunState, LongRunTaskObservation } from "./longRunOps.js";
import { hasSecretValue } from "./redact.js";

export const CONTINUATION_WINDOW_MS = 35 * 60_000;
const CLOSEOUT_MS = 30 * 60_000;
const identity = z.string().min(3).max(160).regex(/^[a-zA-Z0-9_:-]+$/);
const timestamp = z.string().datetime({ offset: true });
const evidence = z.string().trim().min(1).refine((v) => Buffer.byteLength(v, "utf8") <= 2000 && !hasSecretValue(v), "Expected bounded, non-secret evidence");
const windowSchema = z.object({
  invocationId: identity,
  sequence: z.number().int().positive(),
  source: z.enum(["chat", "scheduled"]),
  status: z.enum(["active", "yielded", "abandoned"]),
  startedAt: timestamp,
  deadline: timestamp,
  endedAt: timestamp.optional(),
  reconciledAt: timestamp.optional(),
  nextCheckpoint: evidence.optional()
}).strict();
export const continuationSchema = z.object({
  version: z.literal(1),
  revision: z.number().int().nonnegative(),
  paused: z.boolean(),
  scheduler: z.object({
    requestId: identity,
    status: z.enum(["requested", "bound", "unavailable", "unknown", "disabled"]),
    cadenceMinutes: z.literal(60),
    requestedAt: timestamp,
    automationId: identity.optional(),
    acknowledgedAt: timestamp.optional(),
    failure: evidence.optional()
  }).strict().optional(),
  window: windowSchema.optional(),
  history: z.array(windowSchema).max(24),
  lastScheduledInvocationAt: timestamp.optional()
}).strict();
export type ContinuationState = z.infer<typeof continuationSchema>;
const revision = { expected_revision: z.number().int().nonnegative() };
export const continuationInputSchema = z.discriminatedUnion("operation", [
  z.object({ operation: z.literal("request_schedule"), ...revision }).strict(),
  z.object({ operation: z.literal("bind_schedule"), ...revision, request_id: identity, automation_id: identity, cadence_minutes: z.literal(60) }).strict(),
  z.object({ operation: z.literal("schedule_failed"), ...revision, request_id: identity, outcome: z.enum(["unavailable", "unknown"]), evidence }).strict(),
  z.object({ operation: z.literal("open_window"), ...revision, invocation_id: identity, source: z.enum(["chat", "scheduled"]) }).strict(),
  z.object({ operation: z.literal("yield_window"), ...revision, invocation_id: identity, next_checkpoint: evidence }).strict(),
  z.object({ operation: z.literal("pause"), ...revision }).strict(),
  z.object({ operation: z.literal("resume"), ...revision }).strict(),
  z.object({ operation: z.literal("scheduler_disabled"), ...revision, automation_id: identity }).strict(),
  z.object({ operation: z.literal("scheduler_enabled"), ...revision, automation_id: identity }).strict()
]);

function terminal(run: LongRunState): boolean {
  return ["completed", "failed", "cancelled"].includes(run.status);
}

/** Pure control-state transition. The caller must serialize and persist through LongRunStore.
 * observations are server-derived, never accepted from public tool arguments.
 * A control owner is not an OS permission or a fence for arbitrary external commands.
 */
export function transitionContinuation(run: LongRunState, raw: unknown, observations: LongRunTaskObservation[], now: Date): ContinuationState {
  const input = continuationInputSchema.parse(raw);
  const state: ContinuationState = run.continuation ? structuredClone(run.continuation) : { version: 1, revision: 0, paused: false, history: [] };
  if (input.expected_revision !== state.revision) throw new CodexProError("Continuation revision conflict; reread status before retrying.");
  if (!Number.isFinite(now.getTime())) throw new CodexProError("Invalid continuation clock.");
  const at = now.toISOString();
  if (terminal(run) && !["scheduler_disabled", "bind_schedule", "schedule_failed"].includes(input.operation)) {
    throw new CodexProError("Long run is terminal; only outstanding scheduler receipts may be reconciled.");
  }
  const before = JSON.stringify(state);
  const sched = state.scheduler;
  switch (input.operation) {
    case "request_schedule":
      if (!sched) {
        state.scheduler = {
          requestId: `cr_${createHash("sha256").update(`${run.workspaceId}:${run.runId}:hourly`).digest("hex").slice(0, 32)}`,
          status: "requested", cadenceMinutes: 60, requestedAt: at
        };
      }
      break;
    case "bind_schedule":
      if (!sched || sched.requestId !== input.request_id) throw new CodexProError("Unknown scheduler request identity.");
      if (sched.automationId && sched.automationId !== input.automation_id) throw new CodexProError("Run is already bound to another scheduler; refusing conflicting binding.");
      if (!sched.automationId) {
        sched.automationId = input.automation_id;
        sched.status = "bound";
        sched.acknowledgedAt = at;
        delete sched.failure;
      }
      break;
    case "schedule_failed":
      if (!sched || sched.requestId !== input.request_id || sched.automationId) throw new CodexProError("Scheduler failure does not match an unbound request.");
      sched.status = input.outcome;
      sched.failure = input.evidence;
      break;
    case "open_window": {
      const prior = state.window;
      if (prior?.invocationId === input.invocation_id) {
        if (prior.status !== "active") throw new CodexProError("This invocation already closed its window.");
        if (prior.source !== input.source) throw new CodexProError("Invocation source conflict.");
        break; // No renewed deadline on retry, even when already expired.
      }
      if (state.paused) throw new CodexProError("Continuation is paused.");
      if (state.history.some((w) => w.invocationId === input.invocation_id)) throw new CodexProError("Invocation already used; cannot open a second window.");
      if (prior?.status === "active" && now.getTime() < Date.parse(prior.deadline)) throw new CodexProError("Another active window owns this continuation.");
      // Missing attached observations are unknown, not proof of no live process.
      const observed = new Map(observations.map((o) => [o.taskId, o.status]));
      if (observations.some((o) => ["running", "cancelling", "unknown"].includes(o.status)) ||
          run.taskIds.some((id) => !run.taskResolutions[id] && !["completed", "failed", "cancelled"].includes(observed.get(id) ?? "unknown"))) {
        throw new CodexProError("An active or unknown task requires reconciliation before opening another work window.");
      }
      if (input.source === "scheduled" && sched?.status !== "bound") throw new CodexProError("Scheduled invocation requires a bound scheduler.");
      if (prior) {
        state.history.push(prior.status === "active" ? { ...prior, status: "abandoned", reconciledAt: at } : prior);
        state.history = state.history.slice(-24);
      }
      state.window = {
        invocationId: input.invocation_id, sequence: (prior?.sequence ?? 0) + 1, source: input.source,
        status: "active", startedAt: at, deadline: new Date(now.getTime() + CONTINUATION_WINDOW_MS).toISOString()
      };
      if (input.source === "scheduled") state.lastScheduledInvocationAt = at;
      break;
    }
    case "yield_window": {
      const active = state.window;
      if (!active || active.invocationId !== input.invocation_id) throw new CodexProError("Window owner mismatch.");
      if (active.status === "yielded" && active.nextCheckpoint === input.next_checkpoint) break;
      if (active.status !== "active") throw new CodexProError("Window is no longer active.");
      if (now.getTime() < Date.parse(active.startedAt)) throw new CodexProError("Clock moved backwards; reconcile before closing.");
      active.status = "yielded";
      active.endedAt = at;
      active.nextCheckpoint = input.next_checkpoint;
      break;
    }
    case "pause": state.paused = true; break;
    case "resume": state.paused = false; break;
    case "scheduler_disabled":
      if (!sched || sched.automationId !== input.automation_id) throw new CodexProError("Scheduler identity mismatch.");
      if (!terminal(run) && !state.paused) throw new CodexProError("Pause or complete the run before acknowledging scheduler disable.");
      if (sched.status !== "disabled") { sched.status = "disabled"; sched.acknowledgedAt = at; }
      break;
    case "scheduler_enabled":
      if (!sched || sched.automationId !== input.automation_id) throw new CodexProError("Scheduler identity mismatch.");
      if (state.paused) throw new CodexProError("Resume the continuation before acknowledging scheduler enable.");
      if (sched.status !== "disabled") throw new CodexProError("Scheduler is not awaiting re-enable acknowledgement.");
      sched.status = "bound";
      sched.acknowledgedAt = at;
      delete sched.failure;
      break;
  }
  if (JSON.stringify(state) !== before) state.revision += 1;
  return continuationSchema.parse(state);
}

/** A request/receipt projection, not a hidden HTTP API or an autonomous model executor. */
export function continuationSummary(run: LongRunState, now = new Date()): Record<string, unknown> {
  const state = run.continuation;
  const sched = state?.scheduler;
  const window = state?.window;
  const elapsed = window ? now.getTime() - Date.parse(window.startedAt) : 0;
  let action = "none";
  if ((terminal(run) || state?.paused) && sched?.automationId && sched.status !== "disabled") action = "disable_schedule";
  else if (!terminal(run) && !state?.paused && sched?.automationId && sched.status === "disabled") action = "enable_schedule";
  else if (!terminal(run) && !state?.paused && sched?.status === "requested") action = "ensure_hourly_schedule";
  else if (!terminal(run) && !state?.paused && sched?.status === "unknown") action = "reconcile_schedule";
  else if (!terminal(run) && !state?.paused && sched?.status === "unavailable") action = "scheduler_unavailable";
  return {
    enabled: Boolean(state), revision: state?.revision ?? 0, paused: state?.paused ?? false,
    scheduler: sched ?? null, window: window ?? null,
    last_scheduled_invocation_at: state?.lastScheduledInvocationAt ?? null,
    control_owner_is_filesystem_fence: false,
    remaining_seconds: window?.status === "active" ? Math.max(0, Math.ceil((Date.parse(window.deadline) - now.getTime()) / 1000)) : null,
    closeout_due: window?.status === "active" && elapsed >= CLOSEOUT_MS,
    host_action_required: action,
    scheduler_api_owner: "chatgpt_host",
    binding_is_host_reported: true,
    ...(action === "ensure_hourly_schedule" || action === "reconcile_schedule" ? {
      suggested_schedule: "BEGIN:VEVENT\nRRULE:FREQ=HOURLY;INTERVAL=1\nEND:VEVENT",
      continuation_prompt: `Continue the SAME durable run ${run.runId} in ${run.workspaceRoot} through the connected MCP. Scheduler request marker: ${sched?.requestId}. Read AGENTS.md, current plan, run state, Git and active tasks. Resume unfinished work only. One invocation at most one 35-minute work window; close out from minute 30. Preserve checkpoint and existing task IDs. Never create a parallel run or child schedule. Record a real scheduled invocation receipt. When this run is terminal, disable only its bound scheduler. Do not depend on uploaded Project files.`,
      instruction: "Have the ChatGPT host reconcile by run/request marker, create at most one hourly task if absent, then bind its real returned ID through codexpro action=long_run_update with a continuation payload. Unknown outcomes must be reconciled, never blindly recreated."
    } : {})
  };
}
