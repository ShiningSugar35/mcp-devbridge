import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { chromium } from "playwright";

import { BrowserSupervisor } from "../src/browserSupervisor.js";

function requirePinnedChromium(): void {
  const executable = chromium.executablePath();
  if (!existsSync(executable)) {
    throw new Error(`Pinned Playwright Chromium is not installed at ${executable}`);
  }
}

test("concurrent start calls coalesce onto one owned browser instance", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-browser-concurrent-start-"));
  const supervisor = new BrowserSupervisor({
    runtimeRoot,
    profileId: "concurrent-start-fixture",
    engine: "managed-chromium",
    headed: false,
  });
  try {
    const [first, second] = await Promise.all([supervisor.start(), supervisor.start()]);
    assert.equal(first.connected, true);
    assert.equal(second.connected, true);
    assert.equal(first.instanceId, second.instanceId);
    assert.equal(supervisor.snapshot().instanceId, first.instanceId);
  } finally {
    await supervisor.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("browser supervisor can restart the same owned profile after unexpected context loss", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-browser-restart-"));
  const supervisor = new BrowserSupervisor({
    runtimeRoot,
    profileId: "restart-fixture",
    engine: "managed-chromium",
    headed: false,
  });
  try {
    const first = await supervisor.start();
    assert.equal(first.connected, true);
    const firstInstanceId = first.instanceId;
    await supervisor.persistentContext.close();
    assert.equal(supervisor.snapshot().connected, false);

    const second = await supervisor.start();
    assert.equal(second.connected, true);
    assert.notEqual(second.instanceId, firstInstanceId);
  } finally {
    await supervisor.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});
