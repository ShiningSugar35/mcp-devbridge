import assert from "node:assert/strict";
import test from "node:test";

import {
  decidePersistedTurnPreflight,
  decideSendRecovery,
  transitionSendState,
} from "../src/sendStateMachine.js";

test("send state transitions are monotonic and validated", () => {
  assert.equal(transitionSendState("never_sent", "send_started"), "send_started");
  assert.equal(transitionSendState("send_started", "send_confirmed"), "send_confirmed");
  assert.equal(transitionSendState("send_started", "ambiguous"), "ambiguous");
  assert.throws(
    () => transitionSendState("send_confirmed", "send_started"),
    /invalid send-state transition/i,
  );
});

test("a confirmed send is observed and never automatically resent", () => {
  assert.deepEqual(
    decideSendRecovery({
      intentClass: "mutation",
      sendState: "send_confirmed",
      pageTurnEvidence: "unknown",
    }),
    { action: "observe", userReplyRequired: false, reason: "send_already_confirmed" },
  );
});

test("an uncertain mutation send fails closed", () => {
  assert.deepEqual(
    decideSendRecovery({
      intentClass: "mutation",
      sendState: "send_started",
      pageTurnEvidence: "unknown",
    }),
    {
      action: "pause_ambiguous",
      userReplyRequired: true,
      reason: "ambiguous_side_effect_send",
    },
  );
});

test("a send_started turn may resend only after proof that the turn is absent", () => {
  for (const intentClass of ["read_only", "mutation"] as const) {
    assert.equal(
      decideSendRecovery({
        intentClass,
        sendState: "send_started",
        pageTurnEvidence: "absent",
      }).action,
      "send",
    );
  }
});

test("never_sent still requires absence evidence before browser submission", () => {
  assert.equal(
    decideSendRecovery({
      intentClass: "read_only",
      sendState: "never_sent",
      pageTurnEvidence: "unknown",
    }).action,
    "pause_ambiguous",
  );
  assert.equal(
    decideSendRecovery({
      intentClass: "read_only",
      sendState: "never_sent",
      pageTurnEvidence: "absent",
    }).action,
    "send",
  );
});

test("persisted turn preflight suppresses duplicate confirmed and completed sends", () => {
  const confirmed = {
    local_turn_id: "turn-1",
    prompt_sha256: "hash-1",
    intent_class: "mutation" as const,
    assistant_turn_count_before: 0,
    send_state: "send_confirmed" as const,
    response_state: "generating" as const,
    response_sha256: null,
  };
  assert.deepEqual(
    decidePersistedTurnPreflight({
      persistedTurn: confirmed,
      incomingLocalTurnId: "turn-1",
      incomingPromptSha256: "hash-1",
      incomingIntentClass: "mutation",
    }),
    {
      action: "observe_existing",
      userReplyRequired: false,
      reason: "send_already_confirmed",
    },
  );
  assert.deepEqual(
    decidePersistedTurnPreflight({
      persistedTurn: { ...confirmed, response_state: "complete", response_sha256: "response-1" },
      incomingLocalTurnId: "turn-1",
      incomingPromptSha256: "hash-1",
      incomingIntentClass: "mutation",
    }),
    {
      action: "already_complete",
      userReplyRequired: false,
      reason: "turn_already_complete",
    },
  );
});

test("persisted unfinished turns fail closed unless they are proven never sent", () => {
  const started = {
    local_turn_id: "turn-2",
    prompt_sha256: "hash-2",
    intent_class: "mutation" as const,
    assistant_turn_count_before: 0,
    send_state: "send_started" as const,
    response_state: "waiting_for_turn" as const,
    response_sha256: null,
  };
  assert.equal(
    decidePersistedTurnPreflight({
      persistedTurn: started,
      incomingLocalTurnId: "turn-2",
      incomingPromptSha256: "hash-2",
      incomingIntentClass: "mutation",
    }).action,
    "pause_ambiguous",
  );
  assert.equal(
    decidePersistedTurnPreflight({
      persistedTurn: started,
      incomingLocalTurnId: null,
      incomingPromptSha256: "hash-2",
      incomingIntentClass: "mutation",
    }).reason,
    "unfinished_turn_requires_local_turn_id",
  );
  assert.equal(
    decidePersistedTurnPreflight({
      persistedTurn: { ...started, send_state: "never_sent" },
      incomingLocalTurnId: "turn-2",
      incomingPromptSha256: "hash-2",
      incomingIntentClass: "mutation",
    }).action,
    "resume_never_sent",
  );
});

test("a local turn id cannot be reused with different content or intent", () => {
  const persisted = {
    local_turn_id: "turn-3",
    prompt_sha256: "hash-3",
    intent_class: "read_only" as const,
    assistant_turn_count_before: 0,
    send_state: "send_confirmed" as const,
    response_state: "generating" as const,
    response_sha256: null,
  };
  assert.throws(
    () => decidePersistedTurnPreflight({
      persistedTurn: persisted,
      incomingLocalTurnId: "turn-3",
      incomingPromptSha256: "different",
      incomingIntentClass: "read_only",
    }),
    /prompt hash/,
  );
  assert.throws(
    () => decidePersistedTurnPreflight({
      persistedTurn: persisted,
      incomingLocalTurnId: "turn-3",
      incomingPromptSha256: "hash-3",
      incomingIntentClass: "mutation",
    }),
    /intent class/,
  );
});
