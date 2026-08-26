import { randomUUID } from "node:crypto";
import fsp from "node:fs/promises";
import path from "node:path";
import { z } from "zod";
import type { Workspace } from "./guard.js";
import { CodexProError, PathGuard } from "./guard.js";
import { hasSecretValue } from "./redact.js";

export const LONG_RUN_SCHEMA_VERSION = 1;
export const LONG_RUN_MAX_STEPS = 50;
export const LONG_RUN_MAX_CHECKPOINTS = 200;
export const LONG_RUN_MAX_REVIEWS = 20;
const LONG_RUN_MAX_FILE_BYTES = 512 * 1024;
const LONG_RUN_MAX_LIST = 100;
const TEXT_MAX = 4_000;
const EVIDENCE_MAX = 30;
const CRITERIA_MAX = 30;
const NOTES_MAX = 50;

export type LongRunStatus = "working" | "reviewing" | "rework" | "completed" | "failed" | "cancelled";
export type LongRunStepStatus = "pending" | "in_progress" | "done" | "blocked";
export type LongRunReviewVerdict = "pass" | "fail";
export type LongRunTaskTerminalStatus = "completed" | "failed" | "cancelled";

export interface LongRunStep {
  id: string;
  title: string;
  acceptanceCriteria: string[];
  status: LongRunStepStatus;
  evidence: string[];
  notes: string[];
  updatedAt: string;
}

export interface LongRunCheckpoint {
  at: string;
  type: "plan" | "progress" | "evidence" | "task" | "review" | "rework" | "completion" | "cancel";
  message: string;
  stepId?: string;
  taskId?: string;
}

export interface LongRunReview {
  round: number;
  verdict: LongRunReviewVerdict;
  summary: string;
  failedStepIds: string[];
  failedCriteria: string[];
  requiredRework: string[];
  evidence: string[];
  workRevision: number;
  reviewedAt: string;
}

export interface LongRunTaskResolution {
  status: LongRunTaskTerminalStatus;
  evidence: string;
  resolvedAt: string;
}

export interface LongRunState {
  schemaVersion: number;
  runId: string;
  workspaceId: string;
  workspaceRoot: string;
  title: string;
  objective: string;
  overallAcceptanceCriteria: string[];
  status: LongRunStatus;
  createdAt: string;
  updatedAt: string;
  workRevision: number;
  planRevision: number;
  reviewRound: number;
  steps: LongRunStep[];
  checkpoints: LongRunCheckpoint[];
  taskIds: string[];
  taskResolutions: Record<string, LongRunTaskResolution>;
  reviews: LongRunReview[];
  completion?: { completedAt: string; summary: string };
}

export interface LongRunTaskObservation {
  taskId: string;
  status: "running" | "cancelling" | LongRunTaskTerminalStatus | "unknown";
  detail?: string;
}

export interface LongRunUpdateInput {
  stepId?: unknown;
  stepStatus?: unknown;
  note?: unknown;
  evidence?: unknown;
  taskId?: unknown;
  resolveTaskId?: unknown;
  resolveTaskStatus?: unknown;
  resolveTaskEvidence?: unknown;
  checkpoint?: unknown;
}

export interface LongRunReviewInput {
  verdict: unknown;
  summary: unknown;
  failedStepIds?: unknown;
  failedCriteria?: unknown;
  requiredRework?: unknown;
  evidence?: unknown;
}

const stepSchema = z.object({
  id: z.string(),
  title: z.string(),
  acceptanceCriteria: z.array(z.string()),
  status: z.enum(["pending", "in_progress", "done", "blocked"]),
  evidence: z.array(z.string()),
  notes: z.array(z.string()),
  updatedAt: z.string()
});

const checkpointSchema = z.object({
  at: z.string(),
  type: z.enum(["plan", "progress", "evidence", "task", "review", "rework", "completion", "cancel"]),
  message: z.string(),
  stepId: z.string().optional(),
  taskId: z.string().optional()
});

const reviewSchema = z.object({
  round: z.number().int().min(1),
  verdict: z.enum(["pass", "fail"]),
  summary: z.string(),
  failedStepIds: z.array(z.string()),
  failedCriteria: z.array(z.string()),
  requiredRework: z.array(z.string()),
  evidence: z.array(z.string()),
  workRevision: z.number().int().min(0),
  reviewedAt: z.string()
});

const taskResolutionSchema = z.object({
  status: z.enum(["completed", "failed", "cancelled"]),
  evidence: z.string(),
  resolvedAt: z.string()
});

const longRunSchema = z.object({
  schemaVersion: z.literal(LONG_RUN_SCHEMA_VERSION),
  runId: z.string(),
  workspaceId: z.string(),
  workspaceRoot: z.string(),
  title: z.string(),
  objective: z.string(),
  overallAcceptanceCriteria: z.array(z.string()),
  status: z.enum(["working", "reviewing", "rework", "completed", "failed", "cancelled"]),
  createdAt: z.string(),
  updatedAt: z.string(),
  workRevision: z.number().int().min(0),
  planRevision: z.number().int().min(1),
  reviewRound: z.number().int().min(0),
  steps: z.array(stepSchema),
  checkpoints: z.array(checkpointSchema),
  taskIds: z.array(z.string()),
  taskResolutions: z.record(z.string(), taskResolutionSchema),
  reviews: z.array(reviewSchema),
  completion: z.object({ completedAt: z.string(), summary: z.string() }).optional()
});

function nowIso(): string {
  return new Date().toISOString();
}

function cleanText(value: unknown, label: string, max = TEXT_MAX): string {
  const text = String(value ?? "").trim();
  if (!text) throw new CodexProError(`${label} is required.`);
  if (Buffer.byteLength(text, "utf8") > max) throw new CodexProError(`${label} is too large. Limit: ${max} UTF-8 bytes.`);
  if (hasSecretValue(text)) throw new CodexProError(`${label} appears to contain a secret. Store a redacted evidence reference instead.`);
  return text;
}

function cleanOptionalText(value: unknown, label: string, max = TEXT_MAX): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  if (Buffer.byteLength(text, "utf8") > max) throw new CodexProError(`${label} is too large. Limit: ${max} UTF-8 bytes.`);
  if (hasSecretValue(text)) throw new CodexProError(`${label} appears to contain a secret. Store a redacted evidence reference instead.`);
  return text;
}

function cleanList(values: unknown, label: string, maxItems: number, options: { required?: boolean } = {}): string[] {
  if (!Array.isArray(values)) {
    if (options.required) throw new CodexProError(`${label} must be a non-empty list.`);
    return [];
  }
  const cleaned = values.map((value, index) => cleanText(value, `${label}[${index}]`, 2_000));
  if (cleaned.length > maxItems) throw new CodexProError(`${label} has too many items. Limit: ${maxItems}.`);
  if (options.required && cleaned.length === 0) throw new CodexProError(`${label} must be a non-empty list.`);
  return [...new Set(cleaned)];
}

function runIdValid(runId: string): boolean {
  return /^lr_[a-z0-9_-]{8,96}$/i.test(runId);
}

function assertMutable(state: LongRunState): void {
  if (state.status === "completed" || state.status === "cancelled" || state.status === "failed") {
    throw new CodexProError(`Long run ${state.runId} is terminal (${state.status}) and cannot be modified.`);
  }
}

function pushCheckpoint(state: LongRunState, checkpoint: LongRunCheckpoint): void {
  state.checkpoints.push(checkpoint);
  if (state.checkpoints.length > LONG_RUN_MAX_CHECKPOINTS) {
    state.checkpoints.splice(0, state.checkpoints.length - LONG_RUN_MAX_CHECKPOINTS);
  }
}

function stepById(state: LongRunState, stepId: string): LongRunStep {
  const step = state.steps.find((candidate) => candidate.id === stepId);
  if (!step) throw new CodexProError(`Unknown long-run step: ${stepId}.`);
  return step;
}

function allStepsDone(state: LongRunState): boolean {
  return state.steps.length > 0 && state.steps.every((step) => step.status === "done");
}

function reviewFreshForCurrentWork(state: LongRunState): boolean {
  const review = state.reviews.at(-1);
  return Boolean(review && review.verdict === "pass" && review.workRevision === state.workRevision);
}

function runFileName(runId: string): string {
  if (!runIdValid(runId)) throw new CodexProError(`Invalid long_run_id: ${runId}.`);
  return `${runId}.json`;
}

async function atomicReplace(absPath: string, content: string): Promise<void> {
  const dir = path.dirname(absPath);
  const tmp = path.join(dir, `.${path.basename(absPath)}.${process.pid}.${randomUUID()}.tmp`);
  await fsp.writeFile(tmp, content, { encoding: "utf8", mode: 0o600, flag: "wx" });
  try {
    await fsp.rename(tmp, absPath);
  } catch (error: any) {
    if (!["EEXIST", "EPERM", "ENOTEMPTY"].includes(String(error?.code ?? ""))) throw error;
    await fsp.rm(absPath, { force: true });
    await fsp.rename(tmp, absPath);
  } finally {
    await fsp.rm(tmp, { force: true }).catch(() => undefined);
  }
}

export class LongRunStore {
  private readonly runLocks = new Map<string, Promise<void>>();

  constructor(private readonly contextDir: string, private readonly guard: PathGuard) {}

  private async withRunLock<T>(workspace: Workspace, runId: string, fn: () => Promise<T>): Promise<T> {
    const key = `${workspace.id}:${runId}`;
    const previous = this.runLocks.get(key) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const queued = previous.catch(() => undefined).then(() => gate);
    this.runLocks.set(key, queued);
    await previous.catch(() => undefined);
    try {
      return await fn();
    } finally {
      release();
      if (this.runLocks.get(key) === queued) this.runLocks.delete(key);
    }
  }

  private dirRel(): string {
    return `${this.contextDir.replace(/\\/g, "/").replace(/\/+$/, "")}/long-runs`;
  }

  private async ensureDir(workspace: Workspace): Promise<string> {
    const initial = this.guard.resolve(workspace, this.dirRel(), { forWrite: true });
    await fsp.mkdir(initial.absPath, { recursive: true, mode: 0o700 });
    return this.guard.resolve(workspace, this.dirRel(), { forWrite: true }).absPath;
  }

  private async statePath(workspace: Workspace, runId: string, forWrite: boolean): Promise<string> {
    const rel = `${this.dirRel()}/${runFileName(runId)}`;
    if (forWrite) await this.ensureDir(workspace);
    return this.guard.resolve(workspace, rel, { forWrite }).absPath;
  }

  async read(workspace: Workspace, runId: string): Promise<LongRunState> {
    const absPath = await this.statePath(workspace, runId, false);
    let stat;
    try {
      stat = await fsp.stat(absPath);
    } catch (error: any) {
      if (error?.code === "ENOENT") throw new CodexProError(`Long run not found: ${runId}.`);
      throw error;
    }
    if (!stat.isFile()) throw new CodexProError(`Long run state is not a file: ${runId}.`);
    if (stat.size > LONG_RUN_MAX_FILE_BYTES) throw new CodexProError(`Long run state is too large or corrupt: ${runId}.`);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await fsp.readFile(absPath, "utf8"));
    } catch (error) {
      throw new CodexProError(`Long run state is invalid JSON: ${runId}. ${error instanceof Error ? error.message : String(error)}`);
    }
    const result = longRunSchema.safeParse(parsed);
    if (!result.success) throw new CodexProError(`Long run state is invalid or from an unsupported schema: ${runId}.`);
    if (result.data.workspaceId !== workspace.id || path.resolve(result.data.workspaceRoot) !== path.resolve(workspace.root)) {
      throw new CodexProError(`Long run ${runId} belongs to a different workspace.`);
    }
    return result.data as LongRunState;
  }

  async write(workspace: Workspace, state: LongRunState): Promise<LongRunState> {
    const normalized = longRunSchema.parse(state) as LongRunState;
    const absPath = await this.statePath(workspace, state.runId, true);
    const content = `${JSON.stringify(normalized, null, 2)}\n`;
    if (Buffer.byteLength(content, "utf8") > LONG_RUN_MAX_FILE_BYTES) {
      throw new CodexProError(`Long run state exceeded ${LONG_RUN_MAX_FILE_BYTES} bytes. Reduce evidence/checkpoint volume.`);
    }
    await atomicReplace(absPath, content);
    return normalized;
  }

  async start(
    workspace: Workspace,
    input: {
      title: unknown;
      objective: unknown;
      steps: unknown;
      acceptanceCriteria?: unknown;
    }
  ): Promise<LongRunState> {
    const title = cleanText(input.title, "title", 500);
    const objective = cleanText(input.objective, "objective", 4_000);
    if (!Array.isArray(input.steps) || input.steps.length === 0) throw new CodexProError("steps must be a non-empty list.");
    if (input.steps.length > LONG_RUN_MAX_STEPS) throw new CodexProError(`steps has too many items. Limit: ${LONG_RUN_MAX_STEPS}.`);
    const createdAt = nowIso();
    const steps: LongRunStep[] = input.steps.map((raw: any, index) => ({
      id: `s${index + 1}`,
      title: cleanText(raw?.title, `steps[${index}].title`, 500),
      acceptanceCriteria: cleanList(raw?.acceptance_criteria ?? raw?.acceptanceCriteria, `steps[${index}].acceptance_criteria`, CRITERIA_MAX, { required: true }),
      status: "pending",
      evidence: [],
      notes: [],
      updatedAt: createdAt
    }));
    const runId = `lr_${Date.now().toString(36)}_${randomUUID().replace(/-/g, "").slice(0, 12)}`;
    const state: LongRunState = {
      schemaVersion: LONG_RUN_SCHEMA_VERSION,
      runId,
      workspaceId: workspace.id,
      workspaceRoot: workspace.root,
      title,
      objective,
      overallAcceptanceCriteria: cleanList(input.acceptanceCriteria, "acceptance_criteria", CRITERIA_MAX),
      status: "working",
      createdAt,
      updatedAt: createdAt,
      workRevision: 0,
      planRevision: 1,
      reviewRound: 0,
      steps,
      checkpoints: [{ at: createdAt, type: "plan", message: `Plan created with ${steps.length} steps.` }],
      taskIds: [],
      taskResolutions: {},
      reviews: []
    };
    return this.write(workspace, state);
  }

  async update(workspace: Workspace, runId: string, input: LongRunUpdateInput): Promise<LongRunState> {
    return this.withRunLock(workspace, runId, () => this.updateUnlocked(workspace, runId, input));
  }

  private async updateUnlocked(workspace: Workspace, runId: string, input: LongRunUpdateInput): Promise<LongRunState> {
    const state = await this.read(workspace, runId);
    assertMutable(state);
    const at = nowIso();
    let changedWork = false;
    const stepId = cleanOptionalText(input.stepId, "step_id", 100);
    const note = cleanOptionalText(input.note, "note", 2_000);
    const evidence = cleanList(input.evidence, "evidence", EVIDENCE_MAX);
    const checkpoint = cleanOptionalText(input.checkpoint, "checkpoint", 2_000);
    const taskId = cleanOptionalText(input.taskId, "task_id", 160);

    if (stepId) {
      const step = stepById(state, stepId);
      const statusRaw = cleanOptionalText(input.stepStatus, "step_status", 50);
      if (statusRaw) {
        if (!["pending", "in_progress", "done", "blocked"].includes(statusRaw)) {
          throw new CodexProError(`Invalid step_status: ${statusRaw}.`);
        }
        const next = statusRaw as LongRunStepStatus;
        if (next === "done" && evidence.length === 0 && step.evidence.length === 0) {
          throw new CodexProError(`Step ${stepId} cannot be marked done without evidence.`);
        }
        if (next === "blocked" && !note && !checkpoint) {
          throw new CodexProError(`Step ${stepId} cannot be blocked without a note/checkpoint explaining the blocker.`);
        }
        if (step.status !== next) {
          step.status = next;
          changedWork = true;
        }
      }
      if (note && !step.notes.includes(note)) {
        step.notes.push(note);
        if (step.notes.length > NOTES_MAX) step.notes.splice(0, step.notes.length - NOTES_MAX);
        changedWork = true;
      }
      for (const item of evidence) {
        if (!step.evidence.includes(item)) {
          step.evidence.push(item);
          changedWork = true;
        }
      }
      if (step.evidence.length > EVIDENCE_MAX) step.evidence.splice(0, step.evidence.length - EVIDENCE_MAX);
      step.updatedAt = at;
    } else if (input.stepStatus !== undefined || note || evidence.length) {
      throw new CodexProError("step_id is required when updating step_status, note, or evidence.");
    }

    if (taskId && !state.taskIds.includes(taskId)) {
      state.taskIds.push(taskId);
      changedWork = true;
      pushCheckpoint(state, { at, type: "task", message: `Attached background task ${taskId}.`, taskId, ...(stepId ? { stepId } : {}) });
    }

    const resolveTaskId = cleanOptionalText(input.resolveTaskId, "resolve_task_id", 160);
    if (resolveTaskId) {
      if (!state.taskIds.includes(resolveTaskId)) throw new CodexProError(`Task ${resolveTaskId} is not attached to long run ${runId}.`);
      const status = cleanText(input.resolveTaskStatus, "resolve_task_status", 30) as LongRunTaskTerminalStatus;
      if (!["completed", "failed", "cancelled"].includes(status)) {
        throw new CodexProError("resolve_task_status must be completed, failed, or cancelled.");
      }
      const taskEvidence = cleanText(input.resolveTaskEvidence, "resolve_task_evidence", 2_000);
      state.taskResolutions[resolveTaskId] = { status, evidence: taskEvidence, resolvedAt: at };
      changedWork = true;
      pushCheckpoint(state, { at, type: "task", message: `Resolved task ${resolveTaskId} as ${status}: ${taskEvidence}`, taskId: resolveTaskId });
    }

    if (checkpoint) {
      pushCheckpoint(state, { at, type: evidence.length ? "evidence" : "progress", message: checkpoint, ...(stepId ? { stepId } : {}) });
    }
    if (!changedWork && !checkpoint) throw new CodexProError("long_run_update did not contain any change or checkpoint.");

    if (changedWork) state.workRevision += 1;
    state.updatedAt = at;
    state.status = allStepsDone(state) ? "reviewing" : state.status === "rework" ? "rework" : "working";
    return this.write(workspace, state);
  }

  async review(workspace: Workspace, runId: string, input: LongRunReviewInput): Promise<LongRunState> {
    return this.withRunLock(workspace, runId, () => this.reviewUnlocked(workspace, runId, input));
  }

  private async reviewUnlocked(workspace: Workspace, runId: string, input: LongRunReviewInput): Promise<LongRunState> {
    const state = await this.read(workspace, runId);
    assertMutable(state);
    const verdict = cleanText(input.verdict, "verdict", 20).toLowerCase() as LongRunReviewVerdict;
    if (verdict !== "pass" && verdict !== "fail") throw new CodexProError("verdict must be pass or fail.");
    if (state.reviews.length >= LONG_RUN_MAX_REVIEWS) throw new CodexProError(`Review limit reached (${LONG_RUN_MAX_REVIEWS}). Split or restart the plan instead of looping forever.`);
    const summary = cleanText(input.summary, "summary", 4_000);
    const failedStepIds = cleanList(input.failedStepIds, "failed_step_ids", LONG_RUN_MAX_STEPS);
    const failedCriteria = cleanList(input.failedCriteria, "failed_criteria", CRITERIA_MAX);
    const requiredRework = cleanList(input.requiredRework, "required_rework", CRITERIA_MAX);
    const evidence = cleanList(input.evidence, "review_evidence", EVIDENCE_MAX, { required: true });
    for (const stepId of failedStepIds) stepById(state, stepId);

    if (verdict === "pass") {
      if (!allStepsDone(state)) throw new CodexProError("PASS review rejected: every plan step must be done first.");
      if (failedStepIds.length || failedCriteria.length || requiredRework.length) {
        throw new CodexProError("PASS review rejected: failed/rework fields must be empty.");
      }
    } else if (!requiredRework.length || (!failedStepIds.length && !failedCriteria.length)) {
      throw new CodexProError("FAIL review requires required_rework and at least one failed_step_id or failed_criteria entry.");
    }

    const reviewedAt = nowIso();
    state.reviewRound += 1;
    const review: LongRunReview = {
      round: state.reviewRound,
      verdict,
      summary,
      failedStepIds,
      failedCriteria,
      requiredRework,
      evidence,
      workRevision: state.workRevision,
      reviewedAt
    };
    state.reviews.push(review);
    pushCheckpoint(state, { at: reviewedAt, type: "review", message: `Review round ${review.round}: ${verdict.toUpperCase()} — ${summary}` });

    if (verdict === "pass") {
      state.status = "reviewing";
    } else {
      for (const stepId of failedStepIds) {
        const step = stepById(state, stepId);
        if (step.status === "done") step.status = "pending";
        step.notes.push(`Review ${review.round} requested rework.`);
        step.updatedAt = reviewedAt;
      }
      state.workRevision += 1;
      state.status = "rework";
      pushCheckpoint(state, { at: reviewedAt, type: "rework", message: `Rework required: ${requiredRework.join(" | ")}` });
    }
    state.updatedAt = reviewedAt;
    return this.write(workspace, state);
  }

  completionBlockers(state: LongRunState, taskObservations: LongRunTaskObservation[]): string[] {
    const blockers: string[] = [];
    for (const step of state.steps) {
      if (step.status !== "done") blockers.push(`step ${step.id} is ${step.status}`);
      if (step.status === "done" && step.evidence.length === 0) blockers.push(`step ${step.id} has no evidence`);
    }
    if (!state.reviews.length) blockers.push("no quality review has been recorded");
    if (!reviewFreshForCurrentWork(state)) blockers.push("latest PASS review is missing or stale relative to current work revision");
    for (const observation of taskObservations) {
      if (observation.status === "running" || observation.status === "cancelling") {
        blockers.push(`background task ${observation.taskId} is still ${observation.status}`);
      } else if (observation.status === "unknown" && !state.taskResolutions[observation.taskId]) {
        blockers.push(`background task ${observation.taskId} is unknown after reconnect/restart and has no explicit terminal resolution evidence`);
      }
    }
    return blockers;
  }

  async complete(
    workspace: Workspace,
    runId: string,
    summaryInput: unknown,
    taskObservations: LongRunTaskObservation[]
  ): Promise<LongRunState> {
    return this.withRunLock(workspace, runId, () => this.completeUnlocked(workspace, runId, summaryInput, taskObservations));
  }

  private async completeUnlocked(
    workspace: Workspace,
    runId: string,
    summaryInput: unknown,
    taskObservations: LongRunTaskObservation[]
  ): Promise<LongRunState> {
    const state = await this.read(workspace, runId);
    assertMutable(state);
    const blockers = this.completionBlockers(state, taskObservations);
    if (blockers.length) throw new CodexProError(`Long run ${runId} cannot complete:\n- ${blockers.join("\n- ")}`);
    const completedAt = nowIso();
    const summary = cleanText(summaryInput, "summary", 4_000);
    state.status = "completed";
    state.completion = { completedAt, summary };
    state.updatedAt = completedAt;
    pushCheckpoint(state, { at: completedAt, type: "completion", message: summary });
    return this.write(workspace, state);
  }

  async cancel(workspace: Workspace, runId: string, reasonInput: unknown): Promise<LongRunState> {
    return this.withRunLock(workspace, runId, () => this.cancelUnlocked(workspace, runId, reasonInput));
  }

  private async cancelUnlocked(workspace: Workspace, runId: string, reasonInput: unknown): Promise<LongRunState> {
    const state = await this.read(workspace, runId);
    if (state.status === "completed") throw new CodexProError(`Completed long run ${runId} cannot be cancelled.`);
    if (state.status === "cancelled") return state;
    const at = nowIso();
    const reason = cleanText(reasonInput, "reason", 2_000);
    state.status = "cancelled";
    state.updatedAt = at;
    pushCheckpoint(state, { at, type: "cancel", message: reason });
    return this.write(workspace, state);
  }

  async list(workspace: Workspace): Promise<LongRunState[]> {
    let dirAbs: string;
    try {
      dirAbs = this.guard.resolve(workspace, this.dirRel()).absPath;
    } catch (error: any) {
      if (String(error?.message ?? "").includes("Path")) throw error;
      return [];
    }
    let entries;
    try {
      entries = await fsp.readdir(dirAbs, { withFileTypes: true });
    } catch (error: any) {
      if (error?.code === "ENOENT") return [];
      throw error;
    }
    const ids = entries
      .filter((entry) => entry.isFile() && /^lr_[a-z0-9_-]+\.json$/i.test(entry.name))
      .slice(0, LONG_RUN_MAX_LIST)
      .map((entry) => entry.name.slice(0, -5));
    const states: LongRunState[] = [];
    for (const id of ids) {
      try {
        states.push(await this.read(workspace, id));
      } catch {
        // A corrupt single run must not make the entire list unusable. Direct read remains fail-closed.
      }
    }
    return states.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }
}

export function summarizeLongRun(state: LongRunState, taskObservations: LongRunTaskObservation[] = []): Record<string, unknown> {
  const done = state.steps.filter((step) => step.status === "done").length;
  const latestReview = state.reviews.at(-1);
  const hasBlockedStep = state.steps.some((step) => step.status === "blocked");
  return {
    run_id: state.runId,
    title: state.title,
    objective: state.objective,
    status: state.status,
    progress: { done, total: state.steps.length, percent: state.steps.length ? Math.floor((done / state.steps.length) * 100) : 0 },
    work_revision: state.workRevision,
    plan_revision: state.planRevision,
    review_round: state.reviewRound,
    latest_review: latestReview
      ? { verdict: latestReview.verdict, summary: latestReview.summary, work_revision: latestReview.workRevision, reviewed_at: latestReview.reviewedAt }
      : null,
    steps: state.steps.map((step) => ({
      id: step.id,
      title: step.title,
      acceptance_criteria: step.acceptanceCriteria,
      status: step.status,
      evidence_count: step.evidence.length,
      evidence_tail: step.evidence.slice(-5),
      note_tail: step.notes.slice(-3),
      updated_at: step.updatedAt
    })),
    acceptance_criteria: state.overallAcceptanceCriteria,
    task_ids: state.taskIds,
    task_observations: taskObservations,
    task_resolutions: state.taskResolutions,
    checkpoint_tail: state.checkpoints.slice(-12),
    created_at: state.createdAt,
    updated_at: state.updatedAt,
    completion: state.completion ?? null,
    next_poll_after_seconds: taskObservations.some((task) => task.status === "running" || task.status === "cancelling") ? 30 : 0,
    autonomous_continuation: {
      recommended: state.status === "working" && !hasBlockedStep,
      user_reply_required: hasBlockedStep ? null : false,
      progress_update_after_seconds: 60
    },
    poll_guidance: "Keep each wait_task/get_task request bounded, but continue autonomously in the same assistant turn while the user goal is still actionable. The user does not need to say continue merely because attached background work is running. After a real reconnect, recover with long_run_status from the durable JSON."
  };
}
