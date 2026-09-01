import assert from "node:assert/strict";
import test from "node:test";

import { RecoveryCoordinator, type RecoveryInput } from "../src/recoveryCoordinator.js";

function input(overrides: Partial<RecoveryInput> = {}): RecoveryInput {
  return {
    expectedConversationRef: "conversation-1",
    observedConversationRef: "conversation-1",
    intentClass: "read_only",
    sendState: "send_confirmed",
    pageTurnEvidence: "present",
    responseState: "generating",
    tabState: "open",
    browserState: "connected",
    networkState: "online",
    selectorState: "primary",
    loginGate: false,
    securityGate: false,
    policyGate: false,
    userReplyRequired: false,
    nextPromptExists: false,
    ...overrides,
  };
}

test("recovery decision table is fail-closed at user, identity, selector, and send gates", () => {
  const coordinator = new RecoveryCoordinator();
  assert.equal(coordinator.decide(input({ loginGate: true })).action, "pause_user");
  assert.equal(coordinator.decide(input({ securityGate: true })).action, "pause_user");
  assert.equal(coordinator.decide(input({ policyGate: true })).action, "pause_user");
  assert.equal(coordinator.decide(input({ userReplyRequired: true })).action, "pause_user");
  assert.equal(
    coordinator.decide(input({ observedConversationRef: "conversation-2" })).action,
    "pause_identity",
  );
  assert.equal(coordinator.decide(input({ selectorState: "missing" })).action, "pause_page_broken");
  assert.equal(
    coordinator.decide(
      input({ intentClass: "mutation", sendState: "send_started", pageTurnEvidence: "unknown" }),
    ).action,
    "pause_ambiguous",
  );
});

test("tab, browser, controller, and network faults recover within their smallest domain", () => {
  const coordinator = new RecoveryCoordinator();
  assert.equal(coordinator.decide(input({ tabState: "closed" })).action, "recover_tab");
  assert.equal(coordinator.decide(input({ browserState: "disconnected" })).action, "restart_browser");
  assert.equal(coordinator.decide(input({ controllerRestarted: true })).action, "reload_conversation");
  assert.equal(coordinator.decide(input({ networkState: "degraded" })).action, "wait_for_network");
});

test("same-conversation resume observes a confirmed partial response without resend", () => {
  const coordinator = new RecoveryCoordinator();
  assert.deepEqual(coordinator.decide(input({ responseState: "generating" })), {
    action: "observe",
    safeToAutoContinue: false,
    userReplyRequired: false,
    reason: "response_in_progress",
  });
});

test("automatic continuation is allowed only after a complete turn with a next prompt", () => {
  const coordinator = new RecoveryCoordinator();
  assert.deepEqual(
    coordinator.decide(input({ responseState: "complete", nextPromptExists: true })),
    {
      action: "continue",
      safeToAutoContinue: true,
      userReplyRequired: false,
      reason: "continuation_ready",
    },
  );
});
