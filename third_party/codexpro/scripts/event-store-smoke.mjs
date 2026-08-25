import assert from "node:assert/strict";
import { BoundedInMemoryEventStore } from "../dist/eventStore.js";

const store = new BoundedInMemoryEventStore(4, 16 * 1024, 60_000);
const first = await store.storeEvent("stream-a", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "one" }
});
const second = await store.storeEvent("stream-a", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "two" }
});
await store.storeEvent("stream-b", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "other" }
});
const fourth = await store.storeEvent("stream-a", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "four" }
});

const replayed = [];
const replayStream = await store.replayEventsAfter(first, {
  send: async (eventId, message) => replayed.push({ eventId, message })
});
assert.equal(replayStream, "stream-a");
assert.deepEqual(replayed.map((entry) => entry.eventId), [second, fourth]);
assert.equal(replayed[0].message.params.data, "two");
assert.ok(store.stats().events <= 4);
assert.ok(store.stats().bytes <= 16 * 1024);

const evicting = new BoundedInMemoryEventStore(2, 16 * 1024, 60_000);
const evicted = await evicting.storeEvent("stream-c", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "old" }
});
await evicting.storeEvent("stream-c", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "mid" }
});
await evicting.storeEvent("stream-c", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "new" }
});
assert.equal(evicting.stats().events, 2);
await assert.rejects(
  () => evicting.replayEventsAfter(evicted, { send: async () => {} }),
  /not retained/i
);

const bounded = new BoundedInMemoryEventStore(4, 1024, 60_000);
const beforeGap = await bounded.storeEvent("stream-d", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "before" }
});
const gap = await bounded.storeEvent("stream-d", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "Z".repeat(2048) }
});
const afterGap = await bounded.storeEvent("stream-d", {
  jsonrpc: "2.0",
  method: "notifications/message",
  params: { level: "info", data: "after" }
});
assert.ok(bounded.stats().bytes <= 1024);
const afterReplay = [];
await bounded.replayEventsAfter(gap, {
  send: async (eventId, message) => afterReplay.push({ eventId, message })
});
assert.deepEqual(afterReplay.map((entry) => entry.eventId), [afterGap]);
await assert.rejects(
  () => bounded.replayEventsAfter(beforeGap, { send: async () => {} }),
  /replay gap/i
);

console.log("event-store-smoke: ok");
