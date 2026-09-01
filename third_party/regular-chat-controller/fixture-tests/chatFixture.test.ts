import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import { chromium } from "playwright";

import { CompletionDetector } from "../src/completionDetector.js";
import { chatFixtureHtml } from "../src/fixtures/chatFixture.js";
import { createHash } from "node:crypto";

interface FixtureSnapshot {
  assistantText: string;
  assistantTurnCount: number;
  generationControlPresent: boolean;
  composerReady: boolean;
  finalControlsPresent: boolean;
  selectorMode: "primary" | "fallback" | "missing";
  networkState: "online" | "degraded";
  tabClosed: boolean;
  browserDisconnected: boolean;
}

function hashText(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

async function browserOrExplain() {
  const executable = chromium.executablePath();
  if (!existsSync(executable)) {
    throw new Error(
      `Pinned Playwright Chromium is not installed at ${executable}. Run PLAYWRIGHT_BROWSERS_PATH=<DevBridge regular-chat browsers dir> npm exec playwright install chromium before fixture acceptance.`,
    );
  }
  return chromium.launch({ headless: true });
}

async function fixtureSnapshot(page: import("playwright").Page): Promise<FixtureSnapshot> {
  return page.evaluate(() => {
    const api = (window as unknown as { fixture: { snapshot(): FixtureSnapshot } }).fixture;
    return api.snapshot();
  });
}

async function fixtureCall(page: import("playwright").Page, method: string, arg?: string): Promise<void> {
  await page.evaluate(
    ({ methodName, value }) => {
      const api = (window as unknown as { fixture: Record<string, (...args: string[]) => unknown> }).fixture;
      const fn = api[methodName];
      if (typeof fn !== "function") {
        throw new Error(`unknown fixture method: ${methodName}`);
      }
      if (value === undefined) {
        fn();
      } else {
        fn(value);
      }
    },
    { methodName: method, value: arg },
  );
}

test("fixture proves stable thinking is not completion and late final controls are", async () => {
  const browser = await browserOrExplain();
  try {
    const page = await browser.newPage();
    await page.setContent(chatFixtureHtml());
    const detector = new CompletionDetector({ stableWindowMs: 50 });

    await fixtureCall(page, "delayedAssistant", "thinking");
    let snapshot = await fixtureSnapshot(page);
    assert.equal(
      detector.evaluate({
        nowMs: 1_000,
        assistantTurnCountBefore: 0,
        assistantTurnCountAfter: snapshot.assistantTurnCount,
        assistantText: snapshot.assistantText,
        assistantTextHash: hashText(snapshot.assistantText),
        generationControlPresent: snapshot.generationControlPresent,
        composerReady: snapshot.composerReady,
        finalControlsPresent: snapshot.finalControlsPresent,
        selectorState: snapshot.selectorMode,
        networkState: snapshot.networkState,
      }).state,
      "generating",
    );

    await fixtureCall(page, "stabilizeThinking", "thinking stable");
    snapshot = await fixtureSnapshot(page);
    assert.equal(
      detector.evaluate({
        nowMs: 20_000,
        assistantTurnCountBefore: 0,
        assistantTurnCountAfter: snapshot.assistantTurnCount,
        assistantText: snapshot.assistantText,
        assistantTextHash: hashText(snapshot.assistantText),
        generationControlPresent: snapshot.generationControlPresent,
        composerReady: snapshot.composerReady,
        finalControlsPresent: snapshot.finalControlsPresent,
        selectorState: snapshot.selectorMode,
        networkState: snapshot.networkState,
      }).complete,
      false,
    );

    await fixtureCall(page, "stream", "final answer");
    await fixtureCall(page, "generationEnded");
    snapshot = await fixtureSnapshot(page);
    const candidate = detector.evaluate({
      nowMs: 21_000,
      assistantTurnCountBefore: 0,
      assistantTurnCountAfter: snapshot.assistantTurnCount,
      assistantText: snapshot.assistantText,
      assistantTextHash: hashText(snapshot.assistantText),
      generationControlPresent: snapshot.generationControlPresent,
      composerReady: snapshot.composerReady,
      finalControlsPresent: snapshot.finalControlsPresent,
      selectorState: snapshot.selectorMode,
      networkState: snapshot.networkState,
    });
    assert.equal(candidate.state, "candidate_complete");

    await fixtureCall(page, "showFinalControls");
    snapshot = await fixtureSnapshot(page);
    assert.equal(
      detector.evaluate({
        nowMs: 21_100,
        assistantTurnCountBefore: 0,
        assistantTurnCountAfter: snapshot.assistantTurnCount,
        assistantText: snapshot.assistantText,
        assistantTextHash: hashText(snapshot.assistantText),
        generationControlPresent: snapshot.generationControlPresent,
        composerReady: snapshot.composerReady,
        finalControlsPresent: snapshot.finalControlsPresent,
        selectorState: snapshot.selectorMode,
        networkState: snapshot.networkState,
      }).complete,
      true,
    );
  } finally {
    await browser.close();
  }
});

test("fixture exposes selector drift, network loss, tab loss, and browser disconnect independently", async () => {
  const browser = await browserOrExplain();
  try {
    const page = await browser.newPage();
    await page.setContent(chatFixtureHtml());
    await fixtureCall(page, "driftSelectors");
    assert.equal((await fixtureSnapshot(page)).selectorMode, "fallback");
    await fixtureCall(page, "offline");
    assert.equal((await fixtureSnapshot(page)).networkState, "degraded");
    await fixtureCall(page, "closeTab");
    assert.equal((await fixtureSnapshot(page)).tabClosed, true);
    await fixtureCall(page, "browserDisconnect");
    assert.equal((await fixtureSnapshot(page)).browserDisconnected, true);
  } finally {
    await browser.close();
  }
});
