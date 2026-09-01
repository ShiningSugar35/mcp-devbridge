import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_ACTIVE_CHAT_TABS,
  MAX_GENERATING_CHAT_TABS,
  MAX_LOG_EVENTS,
  MAX_POLL_INTERVAL_MS,
  MAX_SESSION_EVENTS,
  MAX_SESSION_STATE_BYTES,
  MIN_POLL_INTERVAL_MS,
} from "../src/limits.js";

test("all in-memory, state, log, and polling resources have hard limits", () => {
  assert.ok(MAX_ACTIVE_CHAT_TABS > 0 && MAX_ACTIVE_CHAT_TABS <= 16);
  assert.ok(MAX_GENERATING_CHAT_TABS > 0 && MAX_GENERATING_CHAT_TABS <= MAX_ACTIVE_CHAT_TABS);
  assert.ok(MAX_SESSION_STATE_BYTES > 0 && MAX_SESSION_STATE_BYTES <= 1024 * 1024);
  assert.ok(MAX_SESSION_EVENTS > 0 && MAX_SESSION_EVENTS <= 1_000);
  assert.ok(MAX_LOG_EVENTS > 0 && MAX_LOG_EVENTS <= 10_000);
  assert.ok(MIN_POLL_INTERVAL_MS >= 100);
  assert.ok(MAX_POLL_INTERVAL_MS >= MIN_POLL_INTERVAL_MS && MAX_POLL_INTERVAL_MS <= 30_000);
});
