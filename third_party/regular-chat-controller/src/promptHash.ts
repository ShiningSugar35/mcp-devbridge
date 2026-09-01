import { createHash } from "node:crypto";

import type { TurnIdentity } from "./types.js";

export function normalizePrompt(prompt: string): string {
  return prompt.replace(/\r\n?/g, "\n");
}

export function promptSha256(prompt: string): string {
  return createHash("sha256").update(normalizePrompt(prompt), "utf8").digest("hex");
}

export function createTurnIdentity(localTurnId: string, prompt: string): TurnIdentity {
  if (!localTurnId) {
    throw new Error("local turn id is required");
  }
  return {
    localTurnId,
    promptSha256: promptSha256(prompt),
  };
}
