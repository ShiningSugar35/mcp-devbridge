import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { chromium, type Page } from "playwright";

import type { ProviderAdapter, ProviderObservation } from "../src/adapters/provider.js";
import { RegularChatController } from "../src/controller.js";

function requirePinnedChromium(): void {
  const executable = chromium.executablePath();
  if (!existsSync(executable)) throw new Error(`Pinned Playwright Chromium is not installed at ${executable}`);
}

class FixtureAdapter implements ProviderAdapter {
  readonly providerId = "fixture";
  conversationRef: string | null = null;
  assistantTurnCount = 0;
  assistantText = "";
  generationControlPresent = false;
  composerReady = true;
  finalControlsPresent = false;
  networkState: ProviderObservation["networkState"] = "online";
  sendCount = 0;
  closePageOnNextObserve = false;
  closeContextOnNextObserve = false;
  degradedObservationsRemaining = 0;

  async openHome(page: Page): Promise<void> {
    await page.setContent("<main><textarea></textarea></main>");
  }

  async openConversation(page: Page, conversationUrl: string): Promise<void> {
    const ref = /^https:\/\/chatgpt\.com\/c\/([A-Za-z0-9_-]+)/.exec(conversationUrl)?.[1];
    if (!ref) throw new Error("fixture conversation URL invalid");
    if (this.conversationRef !== null && this.conversationRef !== ref) {
      throw new Error("fixture conversation identity mismatch");
    }
    this.conversationRef = ref;
    await page.setContent("<main><textarea></textarea></main>");
  }

  async observe(page: Page): Promise<ProviderObservation> {
    if (this.closeContextOnNextObserve) {
      this.closeContextOnNextObserve = false;
      await page.context().close();
      throw new Error("fixture browser context lost");
    }
    if (this.closePageOnNextObserve) {
      this.closePageOnNextObserve = false;
      await page.close();
      throw new Error("fixture tab lost");
    }
    if (this.degradedObservationsRemaining > 0) {
      this.degradedObservationsRemaining -= 1;
      return { ...this.snapshot(), networkState: "degraded" };
    }
    return this.snapshot();
  }

  async sendPrompt(_page: Page, _prompt: string): Promise<void> {
    this.sendCount += 1;
    this.conversationRef ??= "fixture-conversation-1";
    this.assistantTurnCount += 1;
    this.assistantText = "thinking";
    this.generationControlPresent = true;
    this.composerReady = false;
    this.finalControlsPresent = false;
  }

  async extractMarkdown(_page: Page): Promise<string> {
    return this.assistantText;
  }

  finish(text = "final answer"): void {
    this.assistantText = text;
    this.generationControlPresent = false;
    this.composerReady = true;
    this.finalControlsPresent = true;
  }

  private snapshot(): ProviderObservation {
    return {
      conversationRef: this.conversationRef,
      conversationUrl: this.conversationRef
        ? `https://chatgpt.com/c/${this.conversationRef}`
        : "https://chatgpt.com/",
      assistantTurnCount: this.assistantTurnCount,
      assistantText: this.assistantText,
      generationControlPresent: this.generationControlPresent,
      composerReady: this.composerReady,
      finalControlsPresent: this.finalControlsPresent,
      selectorState: "primary",
      networkState: this.networkState,
      loginGate: false,
      securityGate: false,
      policyGate: false,
    };
  }
}

function options(runtimeRoot: string, adapter: FixtureAdapter) {
  return {
    runtimeRoot,
    profileId: "controller-recovery-fixture",
    engine: "managed-chromium" as const,
    headed: false,
    pollIntervalMs: 250,
    stableOutputWindowMs: 50,
    adapter,
  };
}

const workspaceHash = "a".repeat(64);
const runId = "lr_controller_recovery";
const localTurnId = "turn-controller-recovery-1";

async function openAndSend(controller: RegularChatController, adapter: FixtureAdapter): Promise<void> {
  const opened = await controller.openRun({ workspaceHash, runId });
  assert.equal((opened as { ok: boolean }).ok, true);
  const sent = await controller.send({ runId, prompt: "fixture prompt", localTurnId, intentClass: "read_only" });
  assert.equal((sent as { ok: boolean }).ok, true);
  assert.equal(adapter.sendCount, 1);
}

test("active-tab rejection closes the speculative page instead of leaking browser resources", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-tab-cap-"));
  const adapter = new FixtureAdapter();
  const controller = new RegularChatController(options(runtimeRoot, adapter));
  try {
    await controller.start();
    for (let index = 0; index < 4; index += 1) {
      await controller.openRun({ workspaceHash, runId: `lr_tab_cap_${index}` });
    }
    const before = controller.status() as { browser: { pageCount: number }; activeRuns: unknown[] };
    assert.equal(before.activeRuns.length, 4);
    await assert.rejects(
      controller.openRun({ workspaceHash, runId: "lr_tab_cap_rejected" }),
      /active tab limit exceeded/,
    );
    const after = controller.status() as { browser: { pageCount: number }; activeRuns: unknown[] };
    assert.equal(after.activeRuns.length, 4);
    assert.equal(after.browser.pageCount, before.browser.pageCount);
  } finally {
    await controller.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("generating-cap rejection leaves the unsent turn durably never_sent", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-generating-cap-"));
  const adapter = new FixtureAdapter();
  const controller = new RegularChatController(options(runtimeRoot, adapter));
  const runIds = ["lr_generating_cap_1", "lr_generating_cap_2", "lr_generating_cap_3"];
  try {
    for (const currentRunId of runIds) {
      await controller.openRun({ workspaceHash, runId: currentRunId });
    }
    await controller.send({
      runId: runIds[0]!,
      prompt: "first",
      localTurnId: "turn-generating-cap-1",
      intentClass: "read_only",
    });
    adapter.composerReady = true;
    await controller.send({
      runId: runIds[1]!,
      prompt: "second",
      localTurnId: "turn-generating-cap-2",
      intentClass: "read_only",
    });
    adapter.composerReady = true;
    await assert.rejects(
      controller.send({
        runId: runIds[2]!,
        prompt: "third",
        localTurnId: "turn-generating-cap-3",
        intentClass: "mutation",
      }),
      /generating tab limit exceeded/,
    );
    assert.equal(adapter.sendCount, 2);
    const persistedPath = path.join(runtimeRoot, "sessions", workspaceHash, `${runIds[2]}.json`);
    const persisted = JSON.parse(await readFile(persistedPath, "utf8")) as {
      current_turn: { send_state: string };
    };
    assert.equal(persisted.current_turn.send_state, "never_sent");
  } finally {
    await controller.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("concurrent duplicate send calls serialize and submit exactly once", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-concurrent-send-"));
  const adapter = new FixtureAdapter();
  const controller = new RegularChatController(options(runtimeRoot, adapter));
  try {
    await controller.openRun({ workspaceHash, runId });
    const input = {
      runId,
      prompt: "concurrent fixture prompt",
      localTurnId: "turn-concurrent-send-1",
      intentClass: "mutation" as const,
    };
    const [first, second] = await Promise.all([controller.send(input), controller.send(input)]);
    assert.equal((first as { ok: boolean }).ok, true);
    assert.equal((second as { ok: boolean }).ok, true);
    assert.equal((second as { duplicateSuppressed: boolean }).duplicateSuppressed, true);
    assert.equal(adapter.sendCount, 1);
  } finally {
    await controller.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("controller restart resumes a confirmed turn from persisted assistant baseline without duplicate send", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-restart-"));
  const adapter = new FixtureAdapter();
  const first = new RegularChatController(options(runtimeRoot, adapter));
  let second: RegularChatController | null = null;
  try {
    await openAndSend(first, adapter);
    await first.stop();
    adapter.finish("restart final");

    second = new RegularChatController(options(runtimeRoot, adapter));
    const resumed = await second.resume({ workspaceHash, runId });
    assert.equal((resumed as { ok: boolean }).ok, true);
    const watched = await second.watch({ runId, timeoutMs: 3_000 });
    assert.equal((watched as { complete: boolean }).complete, true);
    assert.equal((watched as { markdown: string }).markdown, "restart final");
    assert.equal(adapter.sendCount, 1);

    const duplicate = await second.send({ runId, prompt: "fixture prompt", localTurnId, intentClass: "read_only" });
    assert.equal((duplicate as { duplicateSuppressed: boolean }).duplicateSuppressed, true);
    assert.equal(adapter.sendCount, 1);
  } finally {
    await first.stop().catch(() => undefined);
    if (second) await second.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("watch automatically rebuilds a closed owned tab and keeps the same conversation", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-tab-"));
  const adapter = new FixtureAdapter();
  const controller = new RegularChatController(options(runtimeRoot, adapter));
  try {
    await openAndSend(controller, adapter);
    adapter.finish("tab recovery final");
    adapter.closePageOnNextObserve = true;
    const watched = await controller.watch({ runId, timeoutMs: 3_000 });
    assert.equal((watched as { complete: boolean }).complete, true);
    assert.equal((watched as { markdown: string }).markdown, "tab recovery final");
    assert.equal(adapter.sendCount, 1);
  } finally {
    await controller.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});

test("watch restarts only the owned browser after context loss and survives short network degradation", async () => {
  requirePinnedChromium();
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "regular-chat-controller-browser-"));
  const adapter = new FixtureAdapter();
  const controller = new RegularChatController(options(runtimeRoot, adapter));
  try {
    await openAndSend(controller, adapter);
    adapter.finish("browser recovery final");
    adapter.closeContextOnNextObserve = true;
    adapter.degradedObservationsRemaining = 2;
    const watched = await controller.watch({ runId, timeoutMs: 5_000 });
    assert.equal((watched as { complete: boolean }).complete, true);
    assert.equal((watched as { markdown: string }).markdown, "browser recovery final");
    assert.equal(adapter.sendCount, 1);
  } finally {
    await controller.stop().catch(() => undefined);
    await rm(runtimeRoot, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});
