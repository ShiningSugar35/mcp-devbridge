import type { ProviderSessionState } from "../src/types.js";

export function makeState(
  overrides: Partial<ProviderSessionState> = {},
): ProviderSessionState {
  return {
    schema_version: 2,
    workspace_hash: "a".repeat(64),
    durable_run_id: "lr_test_001",
    profile_id: "default-managed",
    browser_engine: "managed-chromium",
    browser_instance_id: "browser-001",
    page_id: "page-001",
    conversation_ref: "conversation-001",
    conversation_url: "https://example.invalid/c/conversation-001",
    current_turn: {
      local_turn_id: "turn-001",
      prompt_sha256: "b".repeat(64),
      intent_class: "read_only",
      assistant_turn_count_before: 0,
      send_state: "never_sent",
      response_state: "waiting_for_turn",
      response_sha256: null,
    },
    updated_at: "2026-08-30T00:00:00.000Z",
    ...overrides,
  };
}
