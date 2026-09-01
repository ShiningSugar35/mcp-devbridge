import path from "node:path";

import { RegularChatController } from "./controller.js";
import { JsonLineRpcServer } from "./stdioServer.js";
import type { BrowserEngine } from "./types.js";

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function engineFromEnv(): BrowserEngine {
  const value = (process.env.REGULAR_CHAT_BROWSER_ENGINE ?? "managed-chromium").trim();
  if (value === "managed-chromium" || value === "msedge" || value === "chrome") return value;
  throw new Error(`unsupported browser engine: ${value}`);
}

const runtimeRoot = path.resolve(requiredEnv("REGULAR_CHAT_RUNTIME_ROOT"));
const controller = new RegularChatController({
  runtimeRoot,
  profileId: process.env.REGULAR_CHAT_PROFILE_ID?.trim() || "default-managed",
  engine: engineFromEnv(),
  headed: process.env.REGULAR_CHAT_HEADED !== "0",
});

const server = new JsonLineRpcServer(process.stdin, process.stdout, async (method, params) => {
  const p = (params ?? {}) as Record<string, unknown>;
  switch (method) {
    case "controller.start":
      return controller.start();
    case "controller.status":
      return controller.status();
    case "controller.stop":
      await controller.stop();
      return { ok: true };
    case "profile.login":
      return controller.login();
    case "session.open":
      return controller.openRun({
        workspaceHash: String(p.workspace_hash ?? ""),
        runId: String(p.run_id ?? ""),
        ...(typeof p.conversation_url === "string" && p.conversation_url ? { conversationUrl: p.conversation_url } : {}),
      });
    case "session.resume":
      return controller.resume({
        workspaceHash: String(p.workspace_hash ?? ""),
        runId: String(p.run_id ?? ""),
        ...(typeof p.conversation_url === "string" && p.conversation_url ? { conversationUrl: p.conversation_url } : {}),
      });
    case "session.close":
      return controller.closeRun(String(p.run_id ?? ""));
    case "turn.send":
      return controller.send({
        runId: String(p.run_id ?? ""),
        prompt: String(p.prompt ?? ""),
        ...(typeof p.local_turn_id === "string" && p.local_turn_id ? { localTurnId: p.local_turn_id } : {}),
        ...(p.intent_class === "mutation" ? { intentClass: "mutation" as const } : { intentClass: "read_only" as const }),
      });
    case "turn.watch":
      return controller.watch({
        runId: String(p.run_id ?? ""),
        ...(typeof p.timeout_ms === "number" ? { timeoutMs: p.timeout_ms } : {}),
      });
    case "turn.continue":
      return controller.continueTurn({
        runId: String(p.run_id ?? ""),
        prompt: String(p.prompt ?? ""),
        ...(typeof p.local_turn_id === "string" && p.local_turn_id ? { localTurnId: p.local_turn_id } : {}),
        ...(p.intent_class === "mutation" ? { intentClass: "mutation" as const } : { intentClass: "read_only" as const }),
      });
    default:
      throw new Error(`unknown method: ${method}`);
  }
});

async function shutdown(): Promise<void> {
  server.close();
  await controller.stop().catch(() => undefined);
}

process.once("SIGINT", () => { void shutdown().finally(() => process.exit(0)); });
process.once("SIGTERM", () => { void shutdown().finally(() => process.exit(0)); });

server.run().then(shutdown).catch(async (error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  await controller.stop().catch(() => undefined);
  process.exitCode = 1;
});
