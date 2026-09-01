import type { Locator, Page } from "playwright";

import type { ProviderAdapter, ProviderObservation } from "./provider.js";

const CHATGPT_HOME = "https://chatgpt.com/";
const CONVERSATION_RE = /^https:\/\/chatgpt\.com\/c\/([A-Za-z0-9_-]+)/;

interface LocatorChoice {
  locator: Locator;
  state: "primary" | "fallback";
  selectorIndex: number;
}

async function firstVisible(page: Page, selectors: readonly string[]): Promise<LocatorChoice | null> {
  for (let index = 0; index < selectors.length; index += 1) {
    const locator = page.locator(selectors[index]!).first();
    if ((await locator.count()) > 0 && (await locator.isVisible().catch(() => false))) {
      return { locator, state: index === 0 ? "primary" : "fallback", selectorIndex: index };
    }
  }
  return null;
}

async function isVisibleAny(page: Page, selectors: readonly string[]): Promise<boolean> {
  return (await firstVisible(page, selectors)) !== null;
}

export class ChatGPTAdapter implements ProviderAdapter {
  readonly providerId = "chatgpt-web";

  private readonly composerSelectors = [
    "#prompt-textarea",
    "textarea[placeholder*='Message']",
    "[contenteditable='true'][data-virtualkeyboard='true']",
    "main [contenteditable='true']",
  ] as const;
  private readonly assistantSelectors = [
    "[data-message-author-role='assistant']",
    "main article:has(button[data-testid='copy-turn-action-button'])",
    "main article:has(button[aria-label*='Copy'])",
  ] as const;
  private readonly stopSelectors = [
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop']",
    "button:has-text('Stop generating')",
  ] as const;
  private readonly finalSelectors = [
    "button[data-testid='copy-turn-action-button']",
    "button[aria-label*='Copy']",
    "button:has-text('Copy')",
  ] as const;

  async openHome(page: Page): Promise<void> {
    await page.goto(CHATGPT_HOME, { waitUntil: "domcontentloaded", timeout: 30_000 });
  }

  async openConversation(page: Page, conversationUrl: string): Promise<void> {
    const parsed = new URL(conversationUrl);
    if (parsed.origin !== "https://chatgpt.com" || !parsed.pathname.startsWith("/c/")) {
      throw new Error("conversation URL is not a canonical chatgpt.com conversation URL");
    }
    await page.goto(parsed.toString(), { waitUntil: "domcontentloaded", timeout: 30_000 });
  }

  async observe(page: Page): Promise<ProviderObservation> {
    const url = page.url();
    const conversationRef = CONVERSATION_RE.exec(url)?.[1] ?? null;
    const composer = await firstVisible(page, this.composerSelectors);
    const assistant = await firstVisible(page, this.assistantSelectors);
    const stop = await firstVisible(page, this.stopSelectors);
    const finalControl = await firstVisible(page, this.finalSelectors);
    const assistantLocator = assistant ? page.locator(this.assistantSelectors[assistant.selectorIndex]!) : null;
    const assistantTurnCount = assistantLocator ? await assistantLocator.count() : 0;
    const assistantText = assistantLocator
      ? await assistantLocator.last().innerText().catch(() => "")
      : "";
    const loginGate =
      url.includes("/auth/") ||
      (await isVisibleAny(page, ["button:has-text('Log in')", "button:has-text('Sign up')"]));
    const securityGate = await isVisibleAny(page, [
      "text=/verify you are human/i",
      "text=/security check/i",
      "iframe[src*='challenge']",
      "iframe[title*='challenge']",
    ]);
    const policyGate = await isVisibleAny(page, [
      "text=/unusual activity/i",
      "text=/account.*restricted/i",
      "text=/not available in your region/i",
    ]);
    let selectorState: ProviderObservation["selectorState"] = "missing";
    const criticalStates = [composer?.state, assistant?.state].filter(Boolean);
    const auxiliaryStates = [stop?.state, finalControl?.state].filter(Boolean);
    if (criticalStates.includes("fallback") || auxiliaryStates.includes("fallback")) selectorState = "fallback";
    else if (criticalStates.includes("primary") || auxiliaryStates.includes("primary")) selectorState = "primary";
    const networkState = await page
      .evaluate(() => (navigator.onLine ? "online" : "offline"))
      .catch(() => "degraded" as const);
    return {
      conversationRef,
      conversationUrl: url,
      assistantTurnCount,
      assistantText,
      generationControlPresent: stop !== null,
      composerReady: composer !== null && !loginGate && !securityGate && !policyGate,
      finalControlsPresent: finalControl !== null,
      selectorState,
      networkState,
      loginGate,
      securityGate,
      policyGate,
    };
  }

  async sendPrompt(page: Page, prompt: string): Promise<void> {
    if (!prompt || prompt.length > 120_000) {
      throw new Error("prompt must be non-empty and <= 120000 characters");
    }
    const composer = await firstVisible(page, this.composerSelectors);
    if (!composer) throw new Error("composer selector unavailable");
    await composer.locator.click();
    const tagName = await composer.locator.evaluate((node) => node.tagName.toLowerCase());
    if (tagName === "textarea") {
      await composer.locator.fill(prompt);
    } else {
      await composer.locator.fill(prompt).catch(async () => {
        await composer.locator.pressSequentially(prompt, { delay: 0 });
      });
    }
    await composer.locator.press("Enter");
  }

  async extractMarkdown(page: Page): Promise<string> {
    const assistantChoice = await this.lastAssistantTurn(page);
    if (!assistantChoice) {
      throw new Error("assistant response not found with a safe selector");
    }
    return assistantChoice.locator.evaluate((root) => {
      const escape = (value: string) => value.replace(/\\/g, "\\\\");
      const render = (node: Node, listIndex?: number): string => {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
        if (!(node instanceof HTMLElement)) return "";
        const tag = node.tagName.toLowerCase();
        const children = Array.from(node.childNodes).map((child) => render(child)).join("");
        if (/^h[1-6]$/.test(tag)) return `${"#".repeat(Number(tag[1]))} ${children.trim()}\n\n`;
        if (tag === "p") return `${children.trim()}\n\n`;
        if (tag === "br") return "\n";
        if (tag === "strong" || tag === "b") return `**${children}**`;
        if (tag === "em" || tag === "i") return `*${children}*`;
        if (tag === "code" && node.parentElement?.tagName.toLowerCase() !== "pre") return `\`${escape(children)}\``;
        if (tag === "pre") {
          const code = node.innerText.replace(/\n+$/, "");
          return `\n\`\`\`\n${code}\n\`\`\`\n\n`;
        }
        if (tag === "li") {
          const parentTag = node.parentElement?.tagName.toLowerCase();
          return parentTag === "ol" ? `${listIndex ?? 1}. ${children.trim()}\n` : `- ${children.trim()}\n`;
        }
        if (tag === "ol") {
          return `${Array.from(node.children).map((child, index) => render(child, index + 1)).join("")}\n`;
        }
        if (tag === "ul") return `${Array.from(node.children).map((child) => render(child)).join("")}\n`;
        if (tag === "blockquote") return `${node.innerText.split("\n").map((line) => `> ${line}`).join("\n")}\n\n`;
        if (tag === "a") {
          const href = node.getAttribute("href") ?? "";
          return href ? `[${children || href}](${href})` : children;
        }
        if (tag === "button" || tag === "svg") return "";
        return children;
      };
      return render(root).replace(/\n{3,}/g, "\n\n").trim();
    });
  }

  private async lastAssistantTurn(page: Page): Promise<LocatorChoice | null> {
    for (let index = 0; index < this.assistantSelectors.length; index += 1) {
      const locator = page.locator(this.assistantSelectors[index]!);
      const count = await locator.count();
      if (count > 0) {
        const last = locator.nth(count - 1);
        if (await last.isVisible().catch(() => false)) {
          return { locator: last, state: index === 0 ? "primary" : "fallback", selectorIndex: index };
        }
      }
    }
    return null;
  }
}
