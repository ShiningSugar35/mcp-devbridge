import assert from "node:assert/strict";
import test from "node:test";

import { CompletionDetector, type CompletionSnapshot } from "../src/completionDetector.js";

function snapshot(overrides: Partial<CompletionSnapshot> = {}): CompletionSnapshot {
  return {
    nowMs: 1_000,
    assistantTurnCountBefore: 2,
    assistantTurnCountAfter: 3,
    assistantText: "working",
    assistantTextHash: "hash-1",
    generationControlPresent: true,
    composerReady: false,
    finalControlsPresent: false,
    selectorState: "primary",
    networkState: "online",
    ...overrides,
  };
}

test("quiet thinking content cannot be mistaken for completion", () => {
  const detector = new CompletionDetector({ stableWindowMs: 1_000 });
  assert.equal(detector.evaluate(snapshot()).state, "generating");
  const muchLater = detector.evaluate(snapshot({ nowMs: 20_000 }));
  assert.equal(muchLater.complete, false);
  assert.equal(muchLater.state, "generating");
});

test("completion requires a new turn, generation-end evidence, and a stable window", () => {
  const detector = new CompletionDetector({ stableWindowMs: 1_000 });
  const candidate = detector.evaluate(
    snapshot({ generationControlPresent: false, composerReady: true, nowMs: 1_000 }),
  );
  assert.equal(candidate.state, "candidate_complete");
  assert.equal(candidate.complete, false);
  const complete = detector.evaluate(
    snapshot({
      generationControlPresent: false,
      composerReady: true,
      finalControlsPresent: true,
      nowMs: 2_001,
    }),
  );
  assert.equal(complete.state, "complete");
  assert.equal(complete.complete, true);
});

test("content mutation restarts the stable-output window", () => {
  const detector = new CompletionDetector({ stableWindowMs: 1_000 });
  detector.evaluate(snapshot({ generationControlPresent: false, composerReady: true, nowMs: 1_000 }));
  const changed = detector.evaluate(
    snapshot({
      generationControlPresent: false,
      composerReady: true,
      assistantText: "working more",
      assistantTextHash: "hash-2",
      nowMs: 1_900,
    }),
  );
  assert.equal(changed.state, "candidate_complete");
  assert.equal(changed.stableForMs, 0);
});

test("old turns, degraded network, and missing selectors fail closed", () => {
  const detector = new CompletionDetector({ stableWindowMs: 1_000 });
  assert.equal(
    detector.evaluate(snapshot({ assistantTurnCountAfter: 2 })).state,
    "waiting_for_turn",
  );
  assert.equal(detector.evaluate(snapshot({ networkState: "degraded" })).state, "ambiguous");
  assert.equal(detector.evaluate(snapshot({ selectorState: "missing" })).state, "page_broken");
});

test("selector fallback remains usable and is observable", () => {
  const detector = new CompletionDetector({ stableWindowMs: 100 });
  const result = detector.evaluate(
    snapshot({
      selectorState: "fallback",
      generationControlPresent: false,
      finalControlsPresent: true,
    }),
  );
  assert.equal(result.selectorFallbackUsed, true);
  assert.notEqual(result.state, "page_broken");
});
