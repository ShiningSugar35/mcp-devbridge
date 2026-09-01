import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, rm, stat } from "node:fs/promises";
import path from "node:path";

import {
  MAX_PENDING_SESSION_WRITES,
  MAX_SESSION_STATE_BYTES,
  SESSION_SCHEMA_VERSION,
} from "./limits.js";
import type { ProviderSessionState } from "./types.js";

const HASH_RE = /^[a-f0-9]{64}$/;
const RUN_ID_RE = /^[A-Za-z0-9._-]{1,160}$/;
const PROFILE_ID_RE = /^[A-Za-z0-9._-]{1,128}$/;
const ALLOWED_TOP_LEVEL = new Set([
  "schema_version",
  "workspace_hash",
  "durable_run_id",
  "profile_id",
  "browser_engine",
  "browser_instance_id",
  "page_id",
  "conversation_ref",
  "conversation_url",
  "current_turn",
  "updated_at",
]);
const ALLOWED_TURN_FIELDS = new Set([
  "local_turn_id",
  "prompt_sha256",
  "intent_class",
  "assistant_turn_count_before",
  "send_state",
  "response_state",
  "response_sha256",
]);
const SECRET_KEY_RE = /(?:cookie|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token|session[_-]?token|raw[_-]?(?:prompt|response)|prompt[_-]?text|response[_-]?text)/i;

export class SessionStateError extends Error {}

function assertPlainObject(value: unknown, label: string): asserts value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new SessionStateError(`${label} must be an object`);
  }
}

function assertNoForbiddenFields(value: unknown, pathLabel = "session"): void {
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      assertNoForbiddenFields(value[index], `${pathLabel}[${index}]`);
    }
    return;
  }
  if (typeof value !== "object" || value === null) {
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    if (SECRET_KEY_RE.test(key)) {
      throw new SessionStateError(`forbidden session-state field: ${pathLabel}.${key}`);
    }
    assertNoForbiddenFields(child, `${pathLabel}.${key}`);
  }
}

function assertExactKeys(value: Record<string, unknown>, allowed: ReadonlySet<string>, label: string): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new SessionStateError(`forbidden session-state field: ${label}.${key}`);
    }
  }
}

function assertString(value: unknown, label: string, max = 2048): asserts value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new SessionStateError(`${label} must be a non-empty bounded string`);
  }
}

export function validateSessionState(value: unknown): ProviderSessionState {
  assertPlainObject(value, "session state");
  assertNoForbiddenFields(value);
  assertExactKeys(value, ALLOWED_TOP_LEVEL, "session");

  if (value.schema_version !== SESSION_SCHEMA_VERSION) {
    throw new SessionStateError(`unsupported session schema version: ${String(value.schema_version)}`);
  }
  assertString(value.workspace_hash, "workspace_hash", 64);
  if (!HASH_RE.test(value.workspace_hash)) {
    throw new SessionStateError("workspace_hash must be a lowercase SHA-256");
  }
  assertString(value.durable_run_id, "durable_run_id", 160);
  if (!RUN_ID_RE.test(value.durable_run_id)) {
    throw new SessionStateError("durable_run_id contains unsafe characters");
  }
  assertString(value.profile_id, "profile_id", 128);
  if (!PROFILE_ID_RE.test(value.profile_id)) {
    throw new SessionStateError("profile_id contains unsafe characters");
  }
  if (!new Set(["managed-chromium", "msedge", "chrome"]).has(String(value.browser_engine))) {
    throw new SessionStateError("invalid browser_engine");
  }
  assertString(value.browser_instance_id, "browser_instance_id", 256);
  assertString(value.page_id, "page_id", 256);
  assertString(value.conversation_ref, "conversation_ref", 4096);
  assertString(value.conversation_url, "conversation_url", 8192);
  try {
    const parsed = new URL(value.conversation_url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      throw new Error("unsupported protocol");
    }
  } catch {
    throw new SessionStateError("conversation_url must be an absolute HTTP(S) URL");
  }
  assertString(value.updated_at, "updated_at", 128);
  if (!Number.isFinite(Date.parse(value.updated_at))) {
    throw new SessionStateError("updated_at must be an ISO-compatible timestamp");
  }

  assertPlainObject(value.current_turn, "current_turn");
  assertExactKeys(value.current_turn, ALLOWED_TURN_FIELDS, "current_turn");
  assertString(value.current_turn.local_turn_id, "current_turn.local_turn_id", 256);
  assertString(value.current_turn.prompt_sha256, "current_turn.prompt_sha256", 64);
  if (!HASH_RE.test(value.current_turn.prompt_sha256)) {
    throw new SessionStateError("current_turn.prompt_sha256 must be a lowercase SHA-256");
  }
  if (!new Set(["read_only", "mutation"]).has(String(value.current_turn.intent_class))) {
    throw new SessionStateError("invalid current_turn.intent_class");
  }
  if (
    !Number.isSafeInteger(value.current_turn.assistant_turn_count_before) ||
    Number(value.current_turn.assistant_turn_count_before) < 0 ||
    Number(value.current_turn.assistant_turn_count_before) > 1_000_000
  ) {
    throw new SessionStateError("current_turn.assistant_turn_count_before must be a bounded non-negative integer");
  }
  if (!new Set(["never_sent", "send_started", "send_confirmed", "ambiguous"]).has(String(value.current_turn.send_state))) {
    throw new SessionStateError("invalid current_turn.send_state");
  }
  if (
    !new Set([
      "waiting_for_turn",
      "thinking",
      "generating",
      "candidate_complete",
      "complete",
      "ambiguous",
      "page_broken",
    ]).has(String(value.current_turn.response_state))
  ) {
    throw new SessionStateError("invalid current_turn.response_state");
  }
  if (value.current_turn.response_sha256 !== null) {
    assertString(value.current_turn.response_sha256, "current_turn.response_sha256", 64);
    if (!HASH_RE.test(value.current_turn.response_sha256)) {
      throw new SessionStateError("current_turn.response_sha256 must be null or a lowercase SHA-256");
    }
  }

  return value as unknown as ProviderSessionState;
}

export interface SessionSummary {
  workspaceHash: string;
  durableRunId: string;
  profileId: string;
  browserEngine: ProviderSessionState["browser_engine"];
  conversationRefHash: string;
  sendState: ProviderSessionState["current_turn"] extends infer _T ? string | null : never;
  responseState: string | null;
  updatedAt: string;
}

export function summarizeSession(state: ProviderSessionState): SessionSummary {
  const conversationRefHash = createHash("sha256")
    .update(`${state.conversation_ref}\n${state.conversation_url}`, "utf8")
    .digest("hex")
    .slice(0, 16);
  return {
    workspaceHash: state.workspace_hash.slice(0, 16),
    durableRunId: state.durable_run_id,
    profileId: state.profile_id,
    browserEngine: state.browser_engine,
    conversationRefHash,
    sendState: state.current_turn?.send_state ?? null,
    responseState: state.current_turn?.response_state ?? null,
    updatedAt: state.updated_at,
  };
}

export class ProviderSessionStore {
  private readonly queues = new Map<string, Promise<void>>();
  private pendingWrites = 0;

  constructor(private readonly root: string) {
    if (!path.isAbsolute(root)) {
      throw new SessionStateError("session store root must be absolute");
    }
  }

  get pendingWriteCount(): number {
    return this.pendingWrites;
  }

  pathFor(workspaceHash: string, durableRunId: string): string {
    if (!HASH_RE.test(workspaceHash)) {
      throw new SessionStateError("workspace hash is invalid");
    }
    if (!RUN_ID_RE.test(durableRunId)) {
      throw new SessionStateError("durable run id is invalid");
    }
    return path.join(this.root, workspaceHash, `${durableRunId}.json`);
  }

  async load(workspaceHash: string, durableRunId: string): Promise<ProviderSessionState | null> {
    const target = this.pathFor(workspaceHash, durableRunId);
    let info;
    try {
      info = await stat(target);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return null;
      }
      throw error;
    }
    if (info.size > MAX_SESSION_STATE_BYTES) {
      throw new SessionStateError(`session state exceeds ${MAX_SESSION_STATE_BYTES} byte limit`);
    }
    let decoded: unknown;
    try {
      const raw = await readFile(target, "utf8");
      decoded = JSON.parse(raw);
    } catch (error) {
      if (error instanceof SessionStateError) {
        throw error;
      }
      throw new SessionStateError(`session state is corrupt: ${(error as Error).message}`);
    }
    return validateSessionState(decoded);
  }

  async save(state: ProviderSessionState): Promise<void> {
    const validated = validateSessionState(state);
    const key = `${validated.workspace_hash}/${validated.durable_run_id}`;
    if (this.pendingWrites >= MAX_PENDING_SESSION_WRITES) {
      throw new SessionStateError(`pending session write limit exceeded (${MAX_PENDING_SESSION_WRITES})`);
    }
    this.pendingWrites += 1;
    const previous = this.queues.get(key) ?? Promise.resolve();
    const operation = previous
      .catch(() => undefined)
      .then(async () => this.writeAtomic(validated));
    this.queues.set(key, operation);
    try {
      await operation;
    } finally {
      this.pendingWrites -= 1;
      if (this.queues.get(key) === operation) {
        this.queues.delete(key);
      }
    }
  }

  private async writeAtomic(state: ProviderSessionState): Promise<void> {
    const target = this.pathFor(state.workspace_hash, state.durable_run_id);
    const directory = path.dirname(target);
    await mkdir(directory, { recursive: true });
    const encoded = Buffer.from(`${JSON.stringify(state)}\n`, "utf8");
    if (encoded.byteLength > MAX_SESSION_STATE_BYTES) {
      throw new SessionStateError(`session state exceeds ${MAX_SESSION_STATE_BYTES} byte limit`);
    }
    const temporary = `${target}.tmp-${process.pid}-${randomUUID()}`;
    try {
      const handle = await open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(encoded);
        await handle.sync();
      } finally {
        await handle.close();
      }
      await rename(temporary, target);
    } finally {
      await rm(temporary, { force: true }).catch(() => undefined);
    }
  }
}
