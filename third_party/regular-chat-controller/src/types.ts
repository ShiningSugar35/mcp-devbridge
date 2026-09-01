import type { SESSION_SCHEMA_VERSION } from "./limits.js";

export type BrowserEngine = "managed-chromium" | "msedge" | "chrome";
export type IntentClass = "read_only" | "mutation";
export type SendState = "never_sent" | "send_started" | "send_confirmed" | "ambiguous";
export type PageTurnEvidence = "present" | "absent" | "unknown";
export type SelectorState = "primary" | "fallback" | "missing";
export type NetworkState = "online" | "degraded" | "offline";
export type ResponseState =
  | "waiting_for_turn"
  | "thinking"
  | "generating"
  | "candidate_complete"
  | "complete"
  | "ambiguous"
  | "page_broken";
export type TabLeaseState = "opening" | "ready" | "generating" | "recovering" | "closed";

export interface TurnIdentity {
  localTurnId: string;
  promptSha256: string;
}

export interface PersistedTurnState {
  local_turn_id: string;
  prompt_sha256: string;
  intent_class: IntentClass;
  assistant_turn_count_before: number;
  send_state: SendState;
  response_state: ResponseState;
  response_sha256: string | null;
}

export interface ProviderSessionState {
  schema_version: typeof SESSION_SCHEMA_VERSION;
  workspace_hash: string;
  durable_run_id: string;
  profile_id: string;
  browser_engine: BrowserEngine;
  browser_instance_id: string;
  page_id: string;
  conversation_ref: string;
  conversation_url: string;
  current_turn: PersistedTurnState;
  updated_at: string;
}

export interface TabLease {
  sessionId: string;
  runId: string;
  workspaceId: string;
  profileId: string;
  pageId: string;
  conversationRef: string | null;
  state: TabLeaseState;
}
