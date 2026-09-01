import type { IntentClass, PageTurnEvidence, PersistedTurnState, SendState } from "./types.js";

const ALLOWED_TRANSITIONS: Readonly<Record<SendState, ReadonlySet<SendState>>> = {
  never_sent: new Set(["send_started"]),
  send_started: new Set(["send_confirmed", "ambiguous"]),
  send_confirmed: new Set(),
  ambiguous: new Set(),
};

export function transitionSendState(current: SendState, next: SendState): SendState {
  if (current === next) {
    return current;
  }
  if (!ALLOWED_TRANSITIONS[current].has(next)) {
    throw new Error(`invalid send-state transition: ${current} -> ${next}`);
  }
  return next;
}

export interface SendRecoveryInput {
  intentClass: IntentClass;
  sendState: SendState;
  pageTurnEvidence: PageTurnEvidence;
}

export interface SendRecoveryDecision {
  action: "send" | "observe" | "pause_ambiguous";
  userReplyRequired: boolean;
  reason: string;
}

export function decideSendRecovery(input: SendRecoveryInput): SendRecoveryDecision {
  if (input.sendState === "send_confirmed") {
    return { action: "observe", userReplyRequired: false, reason: "send_already_confirmed" };
  }
  if (input.sendState === "ambiguous") {
    return { action: "pause_ambiguous", userReplyRequired: true, reason: "send_state_ambiguous" };
  }
  if (input.pageTurnEvidence === "present") {
    return { action: "observe", userReplyRequired: false, reason: "turn_already_present" };
  }
  if (input.pageTurnEvidence === "absent") {
    return { action: "send", userReplyRequired: false, reason: "turn_proven_absent" };
  }
  if (input.sendState === "send_started" && input.intentClass === "mutation") {
    return {
      action: "pause_ambiguous",
      userReplyRequired: true,
      reason: "ambiguous_side_effect_send",
    };
  }
  return {
    action: "pause_ambiguous",
    userReplyRequired: true,
    reason: "send_status_not_proven",
  };
}

export interface PersistedTurnPreflightInput {
  persistedTurn: PersistedTurnState | null;
  incomingLocalTurnId: string | null;
  incomingPromptSha256: string;
  incomingIntentClass: IntentClass;
}

export interface PersistedTurnPreflightDecision {
  action: "start_new" | "resume_never_sent" | "observe_existing" | "already_complete" | "pause_ambiguous";
  userReplyRequired: boolean;
  reason: string;
}

export function decidePersistedTurnPreflight(
  input: PersistedTurnPreflightInput,
): PersistedTurnPreflightDecision {
  const turn = input.persistedTurn;
  if (!turn) {
    return { action: "start_new", userReplyRequired: false, reason: "no_persisted_turn" };
  }

  const sameTurn = input.incomingLocalTurnId !== null && input.incomingLocalTurnId === turn.local_turn_id;
  if (sameTurn) {
    if (turn.prompt_sha256 !== input.incomingPromptSha256) {
      throw new Error("local turn id conflicts with persisted prompt hash");
    }
    if (turn.intent_class !== input.incomingIntentClass) {
      throw new Error("local turn id conflicts with persisted intent class");
    }
  }

  if (turn.response_state === "complete") {
    if (sameTurn) {
      return { action: "already_complete", userReplyRequired: false, reason: "turn_already_complete" };
    }
    return { action: "start_new", userReplyRequired: false, reason: "previous_turn_complete" };
  }

  if (input.incomingLocalTurnId === null) {
    return {
      action: "pause_ambiguous",
      userReplyRequired: true,
      reason: "unfinished_turn_requires_local_turn_id",
    };
  }
  if (!sameTurn) {
    return {
      action: "pause_ambiguous",
      userReplyRequired: true,
      reason: "previous_turn_incomplete",
    };
  }

  if (turn.send_state === "never_sent") {
    return { action: "resume_never_sent", userReplyRequired: false, reason: "turn_proven_never_sent" };
  }
  if (turn.send_state === "send_confirmed") {
    return { action: "observe_existing", userReplyRequired: false, reason: "send_already_confirmed" };
  }
  return {
    action: "pause_ambiguous",
    userReplyRequired: true,
    reason: turn.intent_class === "mutation" ? "ambiguous_side_effect_send" : "send_status_not_proven",
  };
}
