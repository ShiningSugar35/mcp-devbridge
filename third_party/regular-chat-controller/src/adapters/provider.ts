import type { Page } from "playwright";

export interface ProviderObservation {
  conversationRef: string | null;
  conversationUrl: string;
  assistantTurnCount: number;
  assistantText: string;
  generationControlPresent: boolean;
  composerReady: boolean;
  finalControlsPresent: boolean;
  selectorState: "primary" | "fallback" | "missing";
  networkState: "online" | "degraded" | "offline";
  loginGate: boolean;
  securityGate: boolean;
  policyGate: boolean;
}

export interface ProviderAdapter {
  readonly providerId: string;
  openHome(page: Page): Promise<void>;
  openConversation(page: Page, conversationUrl: string): Promise<void>;
  observe(page: Page): Promise<ProviderObservation>;
  sendPrompt(page: Page, prompt: string): Promise<void>;
  extractMarkdown(page: Page): Promise<string>;
}
