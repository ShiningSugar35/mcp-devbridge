import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { MAX_SESSION_STATE_BYTES } from "../src/limits.js";
import {
  ProviderSessionStore,
  SessionStateError,
  summarizeSession,
} from "../src/sessionStore.js";
import { makeState } from "./helpers.js";

async function withStore(
  callback: (root: string, store: ProviderSessionStore) => Promise<void>,
): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "regular-chat-session-"));
  try {
    await callback(root, new ProviderSessionStore(root));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("session state is atomically persisted without raw prompt, response, or auth material", async () => {
  await withStore(async (root, store) => {
    const state = makeState();
    await store.save(state);
    assert.deepEqual(await store.load(state.workspace_hash, state.durable_run_id), state);

    const sessionPath = store.pathFor(state.workspace_hash, state.durable_run_id);
    const text = await readFile(sessionPath, "utf8");
    assert.match(text, /prompt_sha256/);
    assert.doesNotMatch(text, /same prompt|assistant answer|cookie|authorization/i);
    assert.ok((await stat(sessionPath)).size <= MAX_SESSION_STATE_BYTES);
    assert.deepEqual(
      (await readdir(path.dirname(sessionPath))).filter((name) => name.includes(".tmp-")),
      [],
    );
    assert.ok(sessionPath.startsWith(root));
  });
});

test("per-session concurrent writes are serialized and bounded", async () => {
  await withStore(async (_root, store) => {
    const states = Array.from({ length: 8 }, (_, index) =>
      makeState({
        current_turn: {
          ...makeState().current_turn,
          local_turn_id: `turn-${index}`,
        },
        updated_at: new Date(index * 1000).toISOString(),
      }),
    );
    await Promise.all(states.map((state) => store.save(state)));
    const loaded = await store.load(states[0]!.workspace_hash, states[0]!.durable_run_id);
    assert.equal(loaded?.current_turn?.local_turn_id, "turn-7");
    assert.equal(store.pendingWriteCount, 0);
  });
});

test("corrupt, oversized, and secret-shaped session states fail closed", async () => {
  await withStore(async (_root, store) => {
    const state = makeState();
    const target = store.pathFor(state.workspace_hash, state.durable_run_id);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, "{broken", "utf8");
    await assert.rejects(
      store.load(state.workspace_hash, state.durable_run_id),
      SessionStateError,
    );

    await writeFile(target, "x".repeat(MAX_SESSION_STATE_BYTES + 1), "utf8");
    await assert.rejects(
      store.load(state.workspace_hash, state.durable_run_id),
      /exceeds.*limit/i,
    );

    const unsafe = { ...state, cookie: "not-allowed" } as typeof state;
    await assert.rejects(store.save(unsafe), /forbidden session-state field/i);
  });
});

test("diagnostic summaries hash conversation metadata instead of exposing URLs", () => {
  const state = makeState();
  const summary = summarizeSession(state);
  const serialized = JSON.stringify(summary);
  assert.equal(summary.conversationRefHash.length, 16);
  assert.doesNotMatch(serialized, /example\.invalid|conversation-001/);
});
