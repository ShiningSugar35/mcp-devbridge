import assert from "node:assert/strict";
import test from "node:test";

import { isSendConfirmationSafe } from "../src/sendConfirmation.js";

test("new-chat send is not confirmed until a durable conversation identity exists", () => {
  assert.equal(
    isSendConfirmationSafe({
      beforeConversationRef: null,
      afterConversationRef: null,
      generationStarted: true,
      assistantTurnStarted: true,
    }),
    false,
  );
  assert.equal(
    isSendConfirmationSafe({
      beforeConversationRef: null,
      afterConversationRef: "conversation-1",
      generationStarted: false,
      assistantTurnStarted: false,
    }),
    true,
  );
});

test("existing-conversation send requires the same conversation identity", () => {
  assert.equal(
    isSendConfirmationSafe({
      beforeConversationRef: "conversation-1",
      afterConversationRef: "conversation-1",
      generationStarted: true,
      assistantTurnStarted: false,
    }),
    true,
  );
  assert.equal(
    isSendConfirmationSafe({
      beforeConversationRef: "conversation-1",
      afterConversationRef: "conversation-2",
      generationStarted: true,
      assistantTurnStarted: true,
    }),
    false,
  );
  assert.equal(
    isSendConfirmationSafe({
      beforeConversationRef: "conversation-1",
      afterConversationRef: null,
      generationStarted: true,
      assistantTurnStarted: true,
    }),
    false,
  );
});
