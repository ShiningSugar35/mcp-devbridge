import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { BashTaskSnapshot } from "./bashOps.js";
import { CodexProError } from "./guard.js";
import type { LongRunState } from "./longRunOps.js";

const DETAIL_CURSOR_MAX_CHARS = 2_048;
const DETAIL_PAGE_DEFAULT_BYTES = 8_192;
const DETAIL_PAGE_MIN_BYTES = 1_024;
const DETAIL_PAGE_MAX_BYTES = 16_384;
const ROLLING_OMITTED_MARKER = "...[earlier output omitted]...\n";
const DETAIL_CURSOR_KEY = randomBytes(32);

type DetailCursorKind = "task_output" | "run_detail";

interface DetailCursorPayload {
  v: 1;
  kind: DetailCursorKind;
  id: string;
  scope: string;
  revision: string;
  offset: number;
}

function parseLimitBytes(value: unknown): number {
  const parsed = value === undefined || value === null ? DETAIL_PAGE_DEFAULT_BYTES : Number(value);
  if (!Number.isInteger(parsed) || parsed < DETAIL_PAGE_MIN_BYTES || parsed > DETAIL_PAGE_MAX_BYTES) {
    throw new CodexProError(
      `detail limit_bytes must be an integer between ${DETAIL_PAGE_MIN_BYTES} and ${DETAIL_PAGE_MAX_BYTES}.`
    );
  }
  return parsed;
}

function cursorSignature(body: string): string {
  return createHmac("sha256", DETAIL_CURSOR_KEY).update(body, "utf8").digest("base64url");
}

function encodeCursor(payload: DetailCursorPayload): string {
  const body = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${body}.${cursorSignature(body)}`;
}

function decodeCursor(
  value: unknown,
  expected: Pick<DetailCursorPayload, "kind" | "id" | "scope">
): DetailCursorPayload | null {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (raw.length > DETAIL_CURSOR_MAX_CHARS) throw new CodexProError("Invalid detail cursor: token is too large.");
  const parts = raw.split(".");
  if (parts.length !== 2 || !parts[0] || !parts[1]) throw new CodexProError("Invalid detail cursor: malformed token.");
  const [body, signature] = parts;
  const expectedSignature = cursorSignature(body);
  const actualBuffer = Buffer.from(signature, "utf8");
  const expectedBuffer = Buffer.from(expectedSignature, "utf8");
  if (actualBuffer.length !== expectedBuffer.length || !timingSafeEqual(actualBuffer, expectedBuffer)) {
    throw new CodexProError("Invalid detail cursor: signature mismatch.");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    throw new CodexProError("Invalid detail cursor: malformed token.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new CodexProError("Invalid detail cursor: expected an object token.");
  }
  const cursor = parsed as Partial<DetailCursorPayload>;
  if (
    cursor.v !== 1 ||
    cursor.kind !== expected.kind ||
    cursor.id !== expected.id ||
    cursor.scope !== expected.scope ||
    !Number.isInteger(cursor.offset) ||
    Number(cursor.offset) < 0 ||
    typeof cursor.revision !== "string" ||
    !cursor.revision
  ) {
    throw new CodexProError("Invalid detail cursor: cursor does not belong to this task/run detail request.");
  }
  return cursor as DetailCursorPayload;
}

function shortHash(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex").slice(0, 24);
}

function utf8Page(value: string, offset: number, limitBytes: number): { text: string; end: number; total: number } {
  const buffer = Buffer.from(value, "utf8");
  if (offset < 0 || offset > buffer.length) throw new CodexProError("Invalid detail cursor: offset is outside retained output.");
  if (offset > 0 && offset < buffer.length && (buffer[offset] & 0xc0) === 0x80) {
    throw new CodexProError("Invalid detail cursor: offset is not aligned to a UTF-8 code point.");
  }
  let end = Math.min(buffer.length, offset + limitBytes);
  while (end > offset && end < buffer.length && (buffer[end] & 0xc0) === 0x80) end -= 1;
  if (end === offset && offset < buffer.length) {
    end = Math.min(buffer.length, offset + 4);
    while (end < buffer.length && (buffer[end] & 0xc0) === 0x80) end += 1;
  }
  return { text: buffer.subarray(offset, end).toString("utf8"), end, total: buffer.length };
}

export function taskDetailRetrievalHint(
  task: BashTaskSnapshot,
  stdoutOmittedBytes: number,
  stderrOmittedBytes: number
): Record<string, unknown> | undefined {
  const availableStreams: string[] = [];
  if (stdoutOmittedBytes > 0 || task.stdoutTruncated) availableStreams.push("stdout");
  if (stderrOmittedBytes > 0 || task.stderrTruncated) availableStreams.push("stderr");
  if (!availableStreams.length) return undefined;
  return {
    via: "codexpro",
    action: "task_output",
    args: { task_id: task.taskId, stream: availableStreams[0], limit_bytes: DETAIL_PAGE_DEFAULT_BYTES },
    available_streams: availableStreams,
    scope: "retained_redacted_buffer",
    note: "Use only when earlier retained task output is needed; follow next_cursor until null."
  };
}

export function taskOutputDetail(
  task: BashTaskSnapshot,
  args: Record<string, unknown>
): { text: string; structured: Record<string, unknown> } {
  const stream = String(args.stream ?? "stdout").trim().toLowerCase();
  if (stream !== "stdout" && stream !== "stderr") {
    throw new CodexProError("task_output stream must be stdout or stderr.");
  }
  const limitBytes = parseLimitBytes(args.limit_bytes);
  const value = stream === "stdout" ? task.stdout : task.stderr;
  const observedTotalBytes = stream === "stdout" ? task.stdoutObservedBytes : task.stderrObservedBytes;
  const streamTruncated = stream === "stdout" ? task.stdoutTruncated : task.stderrTruncated;
  const scope = stream;
  const revision = shortHash(`${task.status}\n${task.finishedAt ?? ""}\n${value}`);
  const cursor = decodeCursor(args.cursor, { kind: "task_output", id: task.taskId, scope });
  if (cursor && cursor.revision !== revision) {
    throw new CodexProError("Task output changed since this cursor was issued; restart task_output pagination from the first page.");
  }
  const offset = cursor?.offset ?? 0;
  const page = utf8Page(value, offset, limitBytes);
  const hasMore = page.end < page.total;
  const nextCursor = hasMore
    ? encodeCursor({ v: 1, kind: "task_output", id: task.taskId, scope, revision, offset: page.end })
    : null;
  const earlierOutputUnavailable = Boolean(streamTruncated || value.startsWith(ROLLING_OMITTED_MARKER));
  const snapshotStable = task.status === "completed" || task.status === "failed" || task.status === "cancelled";
  const structured = {
    task_id: task.taskId,
    stream,
    status: task.status,
    snapshot_stable: snapshotStable,
    retained_scope: "retained_redacted_buffer",
    retained_bytes: page.total,
    observed_total_bytes: observedTotalBytes,
    output_retention_limit_bytes: task.outputRetentionLimitBytes,
    earlier_output_unavailable: earlierOutputUnavailable,
    page_start_byte: offset,
    page_end_byte: page.end,
    page_bytes: Buffer.byteLength(page.text, "utf8"),
    limit_bytes: limitBytes,
    text: page.text,
    has_more: hasMore,
    next_cursor: nextCursor
  };
  const text = [
    `# Task Output ${task.taskId}`,
    "",
    `Stream: ${stream}`,
    `Status: ${task.status}`,
    `Retained bytes: ${page.total}`,
    `Page: ${offset}-${page.end}`,
    earlierOutputUnavailable
      ? "Boundary: earlier output was already dropped by the bounded task rolling buffer and cannot be recovered by pagination."
      : "Boundary: the retained stream starts at the beginning of observed output.",
    snapshotStable
      ? "Snapshot: terminal/stable while this task remains in the task registry."
      : "Snapshot: running/mutable; if output changes, the next cursor is rejected and pagination must restart.",
    "",
    page.text,
    "",
    hasMore ? "More retained output is available via next_cursor." : "End of retained output."
  ].join("\n");
  return { text, structured };
}

function runRevision(state: LongRunState): string {
  return shortHash(
    JSON.stringify({
      updatedAt: state.updatedAt,
      workRevision: state.workRevision,
      planRevision: state.planRevision,
      reviewRound: state.reviewRound,
      checkpointCount: state.checkpoints.length,
      taskResolutionCount: Object.keys(state.taskResolutions).length,
      steps: state.steps.map((step) => [step.id, step.updatedAt, step.acceptanceCriteria.length, step.evidence.length, step.notes.length])
    })
  );
}

function runDetailEntries(
  state: LongRunState,
  args: Record<string, unknown>
): { scope: string; section: string; entries: unknown[]; stepId?: string; field?: string } {
  const section = String(args.section ?? "").trim().toLowerCase();
  if (section === "checkpoints") {
    return { scope: "checkpoints", section, entries: state.checkpoints };
  }
  if (section === "task_resolutions") {
    const entries = Object.entries(state.taskResolutions).map(([taskId, resolution]) => ({ task_id: taskId, ...resolution }));
    return { scope: "task_resolutions", section, entries };
  }
  if (section === "step") {
    const stepId = String(args.step_id ?? "").trim();
    const field = String(args.field ?? "evidence").trim().toLowerCase();
    if (!stepId) throw new CodexProError("run_detail section=step requires step_id.");
    if (field !== "acceptance_criteria" && field !== "evidence" && field !== "notes") {
      throw new CodexProError("run_detail step field must be acceptance_criteria, evidence, or notes.");
    }
    const step = state.steps.find((item) => item.id === stepId);
    if (!step) throw new CodexProError(`Unknown long-run step: ${stepId}.`);
    const entries = field === "acceptance_criteria" ? step.acceptanceCriteria : field === "notes" ? step.notes : step.evidence;
    return { scope: `step:${stepId}:${field}`, section, entries, stepId, field };
  }
  throw new CodexProError("run_detail section must be checkpoints, task_resolutions, or step.");
}

function boundedEntryPage(entries: unknown[], offset: number, limitBytes: number): { entries: unknown[]; end: number; bytes: number } {
  if (!Number.isInteger(offset) || offset < 0 || offset > entries.length) {
    throw new CodexProError("Invalid detail cursor: entry offset is outside the selected long-run section.");
  }
  const page: unknown[] = [];
  let used = 2;
  let index = offset;
  for (; index < entries.length; index += 1) {
    const itemBytes = Buffer.byteLength(JSON.stringify(entries[index]), "utf8");
    if (itemBytes > DETAIL_PAGE_MAX_BYTES) {
      throw new CodexProError(
        `A single long-run detail entry is ${itemBytes} bytes, above the ${DETAIL_PAGE_MAX_BYTES}-byte detail ceiling.`
      );
    }
    const separator = page.length ? 1 : 0;
    if (page.length && used + separator + itemBytes > limitBytes) break;
    if (!page.length && used + itemBytes > limitBytes) {
      throw new CodexProError(`limit_bytes=${limitBytes} is too small for the next long-run detail entry (${itemBytes} bytes).`);
    }
    page.push(entries[index]);
    used += separator + itemBytes;
  }
  return { entries: page, end: index, bytes: used };
}

export function longRunDetailRetrievalHint(state: LongRunState): Record<string, unknown> {
  return {
    via: "codexpro",
    action: "run_detail",
    run_id: state.runId,
    sections: {
      checkpoints: state.checkpoints.length,
      task_resolutions: Object.keys(state.taskResolutions).length,
      steps: state.steps.slice(0, 10).map((step) => ({
        step_id: step.id,
        acceptance_criteria: step.acceptanceCriteria.length,
        evidence: step.evidence.length,
        notes: step.notes.length
      }))
    },
    examples: [
      { run_id: state.runId, section: "checkpoints", limit_bytes: DETAIL_PAGE_DEFAULT_BYTES },
      { run_id: state.runId, section: "step", step_id: state.steps[0]?.id ?? "s1", field: "evidence", limit_bytes: DETAIL_PAGE_DEFAULT_BYTES }
    ],
    note: "Use only when compact long_run output omitted history needed for the current decision."
  };
}

export function longRunDetail(
  state: LongRunState,
  args: Record<string, unknown>
): { text: string; structured: Record<string, unknown> } {
  const limitBytes = parseLimitBytes(args.limit_bytes);
  const selected = runDetailEntries(state, args);
  const revision = runRevision(state);
  const cursor = decodeCursor(args.cursor, { kind: "run_detail", id: state.runId, scope: selected.scope });
  if (cursor && cursor.revision !== revision) {
    throw new CodexProError("Long-run state changed since this cursor was issued; restart run_detail pagination from the first page.");
  }
  const offset = cursor?.offset ?? 0;
  const page = boundedEntryPage(selected.entries, offset, limitBytes);
  const hasMore = page.end < selected.entries.length;
  const nextCursor = hasMore
    ? encodeCursor({ v: 1, kind: "run_detail", id: state.runId, scope: selected.scope, revision, offset: page.end })
    : null;
  const structured: Record<string, unknown> = {
    run_id: state.runId,
    section: selected.section,
    ...(selected.stepId ? { step_id: selected.stepId } : {}),
    ...(selected.field ? { field: selected.field } : {}),
    state_updated_at: state.updatedAt,
    total_count: selected.entries.length,
    start_index: offset,
    returned_count: page.entries.length,
    page_payload_bytes: page.bytes,
    limit_bytes: limitBytes,
    entries: page.entries,
    has_more: hasMore,
    next_cursor: nextCursor
  };
  const text = [
    `# Long Run Detail ${state.runId}`,
    "",
    `Section: ${selected.scope}`,
    `Entries: ${offset}-${page.end} of ${selected.entries.length}`,
    "",
    JSON.stringify(page.entries, null, 2),
    "",
    hasMore ? "More detail is available via next_cursor." : "End of selected long-run detail."
  ].join("\n");
  return { text, structured };
}
