import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import { chromium } from "playwright";

import { ChatGPTAdapter } from "../src/adapters/chatgpt.js";

async function browserOrExplain() {
  const executable = chromium.executablePath();
  if (!existsSync(executable)) {
    throw new Error(
      `Pinned Playwright Chromium is not installed at ${executable}. Run PLAYWRIGHT_BROWSERS_PATH=<DevBridge regular-chat browsers dir> npm exec playwright install chromium before fixture acceptance.`,
    );
  }
  return chromium.launch({ headless: true });
}

function pageHtml(): string {
  return `<!doctype html>
<html><body>
<main>
  <article id="user-turn"><div data-message-author-role="user">user text that must never be extracted</div></article>
  <article id="assistant-turn" data-message-author-role="assistant">
    <h2>Result</h2>
    <p>Hello <strong>world</strong>.</p>
    <ol><li>First</li><li>Second</li></ol>
    <pre>const x = 1;</pre>
    <button data-testid="copy-turn-action-button">Copy</button>
  </article>
  <textarea id="prompt-textarea"></textarea>
</main>
<script>
window.__submitted = [];
document.querySelector('#prompt-textarea').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    window.__submitted.push(event.currentTarget.value);
  }
});
</script>
</body></html>`;
}

test("ChatGPT adapter observes only assistant-owned turns and extracts bounded semantic markdown", async () => {
  const browser = await browserOrExplain();
  try {
    const page = await browser.newPage();
    await page.setContent(pageHtml());
    const adapter = new ChatGPTAdapter();
    const observation = await adapter.observe(page);
    assert.equal(observation.assistantTurnCount, 1);
    assert.match(observation.assistantText, /Hello world/);
    assert.doesNotMatch(observation.assistantText, /user text/);
    assert.equal(observation.composerReady, true);
    assert.equal(observation.selectorState, "primary");

    const markdown = await adapter.extractMarkdown(page);
    assert.match(markdown, /^## Result/m);
    assert.match(markdown, /\*\*world\*\*/);
    assert.match(markdown, /1\. First/);
    assert.match(markdown, /```\nconst x = 1;\n```/);
    assert.doesNotMatch(markdown, /user text/);
  } finally {
    await browser.close();
  }
});

test("ChatGPT adapter selector fallback stays assistant-scoped and send uses the composer only", async () => {
  const browser = await browserOrExplain();
  try {
    const page = await browser.newPage();
    await page.setContent(pageHtml());
    await page.locator("#assistant-turn").evaluate((node) => node.removeAttribute("data-message-author-role"));
    const adapter = new ChatGPTAdapter();
    const observation = await adapter.observe(page);
    assert.equal(observation.assistantTurnCount, 1);
    assert.equal(observation.selectorState, "fallback");
    assert.doesNotMatch(observation.assistantText, /user text/);

    await adapter.sendPrompt(page, "fixture prompt");
    const submitted = await page.evaluate(() => (window as unknown as { __submitted: string[] }).__submitted);
    assert.deepEqual(submitted, ["fixture prompt"]);
  } finally {
    await browser.close();
  }
});

test("ChatGPT adapter fails closed when both composer and assistant identity disappear", async () => {
  const browser = await browserOrExplain();
  try {
    const page = await browser.newPage();
    await page.setContent(pageHtml());
    await page.locator("#prompt-textarea").evaluate((node) => node.remove());
    await page.locator("#assistant-turn").evaluate((node) => {
      node.removeAttribute("data-message-author-role");
      node.querySelectorAll("button").forEach((button) => button.remove());
    });
    const adapter = new ChatGPTAdapter();
    const observation = await adapter.observe(page);
    assert.equal(observation.selectorState, "missing");
    assert.equal(observation.assistantTurnCount, 0);
    await assert.rejects(adapter.extractMarkdown(page), /safe selector/);
  } finally {
    await browser.close();
  }
});
