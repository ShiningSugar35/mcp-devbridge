import assert from "node:assert/strict";
import test from "node:test";

import { TabRegistry } from "../src/tabRegistry.js";

function lease(runId: string, pageId: string, conversationRef: string) {
  return {
    sessionId: `session-${runId}`,
    runId,
    workspaceId: "ws-1",
    profileId: "default-managed",
    pageId,
    conversationRef,
    state: "ready" as const,
  };
}

test("one durable run owns one page and one immutable conversation", () => {
  const registry = new TabRegistry();
  registry.add(lease("run-1", "page-1", "conversation-1"));
  assert.throws(() => registry.add(lease("run-2", "page-1", "conversation-2")), /page.*owned/i);
  assert.throws(() => registry.add(lease("run-1", "page-2", "conversation-1")), /run.*lease/i);
  assert.throws(
    () => registry.assertConversation("run-1", "conversation-other"),
    /conversation identity/i,
  );
  registry.assertConversation("run-1", "conversation-1");
});

test("closing one tab does not alter another run lease", () => {
  const registry = new TabRegistry();
  registry.add(lease("run-1", "page-1", "conversation-1"));
  registry.add(lease("run-2", "page-2", "conversation-2"));
  assert.equal(registry.close("run-1")?.state, "closed");
  assert.equal(registry.get("run-1"), undefined);
  assert.equal(registry.get("run-2")?.pageId, "page-2");
  assert.equal(registry.size, 1);
});

test("registry enforces active and generating tab caps", () => {
  const registry = new TabRegistry({ maxActiveTabs: 2, maxGeneratingTabs: 1 });
  registry.add(lease("run-1", "page-1", "conversation-1"));
  registry.add(lease("run-2", "page-2", "conversation-2"));
  assert.throws(() => registry.add(lease("run-3", "page-3", "conversation-3")), /active tab limit/i);
  registry.setState("run-1", "generating");
  assert.throws(() => registry.setState("run-2", "generating"), /generating tab limit/i);
});
