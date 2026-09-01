import { decideSendRecovery } from "./sendStateMachine.js";
import type {
  IntentClass,
  NetworkState,
  PageTurnEvidence,
  ResponseState,
  SelectorState,
  SendState,
} from "./types.js";

export interface RecoveryInput {
  expectedConversationRef: string | null;
  observedConversationRef: string | null;
  intentClass: IntentClass;
  sendState: SendState;
  pageTurnEvidence: PageTurnEvidence;
  responseState: ResponseState;
  tabState: "open" | "closed";
  browserState: "connected" | "disconnected";
  networkState: NetworkState;
  selectorState: SelectorState;
  loginGate: boolean;
  securityGate: boolean;
  policyGate: boolean;
  userReplyRequired: boolean;
  nextPromptExists: boolean;
  controllerRestarted?: boolean;
}

export interface RecoveryDecision {
  action:
    | "pause_user"
    | "pause_identity"
    | "pause_page_broken"
    | "pause_ambiguous"
    | "recover_tab"
    | "restart_browser"
    | "reload_conversation"
    | "wait_for_network"
    | "send"
    | "observe"
    | "continue"
    | "idle";
  safeToAutoContinue: boolean;
  userReplyRequired: boolean;
  reason: string;
}

export class RecoveryCoordinator {
  decide(input: RecoveryInput): RecoveryDecision {
    if (input.loginGate || input.securityGate || input.policyGate || input.userReplyRequired) {
      return this.decision("pause_user", false, true, "user_or_security_gate");
    }
    if (input.expectedConversationRef !== input.observedConversationRef) {
      return this.decision("pause_identity", false, true, "conversation_identity_mismatch");
    }
    if (input.selectorState === "missing" || input.responseState === "page_broken") {
      return this.decision("pause_page_broken", false, true, "selector_or_page_contract_broken");
    }
    if (input.tabState === "closed") {
      return this.decision("recover_tab", false, false, "owned_tab_closed");
    }
    if (input.browserState === "disconnected") {
      return this.decision("restart_browser", false, false, "owned_browser_disconnected");
    }
    if (input.controllerRestarted) {
      return this.decision("reload_conversation", false, false, "controller_restarted");
    }
    if (input.networkState !== "online") {
      return this.decision("wait_for_network", false, false, "network_degraded");
    }

    const sendDecision = decideSendRecovery({
      intentClass: input.intentClass,
      sendState: input.sendState,
      pageTurnEvidence: input.pageTurnEvidence,
    });
    if (sendDecision.action === "pause_ambiguous") {
      return this.decision("pause_ambiguous", false, true, sendDecision.reason);
    }
    if (sendDecision.action === "send") {
      return this.decision("send", false, false, sendDecision.reason);
    }

    if (input.responseState === "complete") {
      if (input.nextPromptExists) {
        return this.decision("continue", true, false, "continuation_ready");
      }
      return this.decision("idle", false, false, "turn_complete_no_next_prompt");
    }
    if (
      input.responseState === "generating" ||
      input.responseState === "thinking" ||
      input.responseState === "candidate_complete" ||
      input.responseState === "waiting_for_turn"
    ) {
      return this.decision("observe", false, false, "response_in_progress");
    }
    if (input.responseState === "ambiguous") {
      return this.decision("pause_ambiguous", false, true, "response_state_ambiguous");
    }
    return this.decision("observe", false, false, "observe_current_turn");
  }

  private decision(
    action: RecoveryDecision["action"],
    safeToAutoContinue: boolean,
    userReplyRequired: boolean,
    reason: string,
  ): RecoveryDecision {
    return { action, safeToAutoContinue, userReplyRequired, reason };
  }
}
