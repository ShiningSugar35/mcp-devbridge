import assert from "node:assert/strict";
import test from "node:test";

import { SESSION_SCHEMA_VERSION } from "../src/limits.js";
import { validateSessionState } from "../src/sessionStore.js";
import { makeState } from "./helpers.js";

test("provider session persists the pre-send assistant turn count required for restart recovery", () => {
  assert.equal(SESSION_SCHEMA_VERSION, 2);
  const state = makeState();
  assert.equal(state.current_turn.assistant_turn_count_before, 0);
  assert.equal(validateSessionState(state).current_turn.assistant_turn_count_before, 0);
});

test("restart recovery evidence is fail-closed when the pre-send assistant count is missing", () => {
  const state = makeState();
  const broken = JSON.parse(JSON.stringify(state)) as Record<string, unknown>;
  delete (broken.current_turn as Record<string, unknown>).assistant_turn_count_before;
  assert.throws(() => validateSessionState(broken), /assistant_turn_count_before/);
});
