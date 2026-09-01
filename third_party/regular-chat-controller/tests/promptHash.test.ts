import assert from "node:assert/strict";
import test from "node:test";

import { createTurnIdentity, normalizePrompt, promptSha256 } from "../src/promptHash.js";

test("prompt normalization changes only newline encoding", () => {
  assert.equal(normalizePrompt("a\r\nb\rc\n d  "), "a\nb\nc\n d  ");
  assert.notEqual(normalizePrompt("a  b"), normalizePrompt("a b"));
});

test("equivalent newline forms hash identically", () => {
  assert.equal(promptSha256("a\r\nb\r"), promptSha256("a\nb\n"));
});

test("deliberately repeated prompts retain distinct local turn identities", () => {
  const first = createTurnIdentity("turn-1", "same prompt");
  const second = createTurnIdentity("turn-2", "same prompt");
  assert.equal(first.promptSha256, second.promptSha256);
  assert.notEqual(first.localTurnId, second.localTurnId);
});
