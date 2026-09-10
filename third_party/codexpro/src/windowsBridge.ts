import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/server";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import type { CodexProConfig } from "./config.js";
import { redactSensitiveText, redactStructured } from "./redact.js";
import { compatibilityInventory, resolveWindowsCall, searchSnapshotWindows } from "./windowsCompatibility.js";

// Three stable public wrappers; the per-project bridge endpoint and enablement
// are injected by DevBridge. Credentials remain process-local, never tool output.
export const WINDOWS_BRIDGE_TOOL_NAMES = ["windows_backend_status", "windows_list_tools", "windows_call"] as const;
const SNAPSHOT_TTL_MS = 10_000;
const INVENTORY_MAX_BYTES = 524_288;
const RESULT_MAX_BYTES = 6 * 1024 * 1024;
type WindowsProfile = "desktop_ui" | "system_full";
type BridgeStatus = "connected" | "disabled" | "configuration_error" | "unavailable";
const DESKTOP_UI_ALLOWLIST = new Set([
  "Click", "DoubleClick", "Type", "HotKey", "MouseMove", "MouseScroll", "ScrollScreen",
  "App", "Snapshot", "Wait", "GetScreenSize", "CurrentCursorPosition", "SearchWindow", "List",
  // Native UI names in the pinned windows-mcp 0.8.2 inventory. System tools are not added.
  "Screenshot", "Move", "Scroll", "Shortcut", "WaitFor", "MultiSelect", "MultiEdit"
]);
function bridgeProfileFromEnv(): WindowsProfile {
  return (process.env.CODEXPRO_WINDOWS_PROFILE ?? "desktop_ui").trim().toLowerCase() === "system_full" ? "system_full" : "desktop_ui";
}
function profileAllowlist(profile: WindowsProfile): Set<string> {
  if (profile === "system_full") return new Set();
  const allow = new Set(DESKTOP_UI_ALLOWLIST);
  (process.env.CODEXPRO_WINDOWS_DESKTOP_ALLOW ?? "").split(",").map(s => s.trim()).filter(Boolean).forEach(s => allow.add(s));
  return allow;
}
interface BridgeConfig { enabled: boolean; url?: string; token: string; error?: string }
function bridgeConfigFromEnv(): BridgeConfig {
  const flag = (process.env.CODEXPRO_WINDOWS_ENABLED ?? "").trim().toLowerCase();
  const token = process.env.CODEXPRO_WINDOWS_BRIDGE_TOKEN ?? "";
  const rawUrl = (process.env.CODEXPRO_WINDOWS_BRIDGE_URL ?? "").trim();
  if (["0", "false", "off", "no"].includes(flag)) return { enabled: false, token: "" };
  if (flag && !["1", "true", "on", "yes"].includes(flag)) return { enabled: false, token: "", error: "Invalid CODEXPRO_WINDOWS_ENABLED; expected 0 or 1." };
  // Standalone/older engines may omit the new flag, but must explicitly configure a bridge.
  const enabled = Boolean(flag || token || rawUrl);
  if (!enabled) return { enabled: false, token: "" };
  if (Buffer.byteLength(token, "utf8") < 24 || /[\r\n]/.test(token)) return { enabled, token: "", error: "Windows bridge enabled without a valid project credential. Restart this project through DevBridge; do not guess another project's port." };
  if (!rawUrl) return { enabled, token: "", error: "Windows bridge enabled without a project-specific loopback endpoint. Restart this project through DevBridge; do not guess another project's port." };
  try {
    const url = new URL(rawUrl);
    if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "[::1]", "localhost"].includes(url.hostname) || url.username || url.password || url.search || url.hash) throw new Error("invalid endpoint");
    if (url.hostname === "localhost") url.hostname = "127.0.0.1";
    return { enabled, token, url: url.toString().replace(/\/+$/, "") };
  } catch {
    return { enabled, token: "", error: "Windows bridge endpoint must be a loopback HTTP(S) URL without embedded credentials, query, or fragment." };
  }
}
function bridgeCallTimeoutMs(): number {
  const value = Number(process.env.CODEXPRO_WINDOWS_CALL_TIMEOUT_MS ?? 120_000);
  return Number.isFinite(value) ? Math.max(1_000, Math.min(300_000, Math.floor(value))) : 120_000;
}
interface BridgeToolInfo {
  name: string;
  title?: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
  call_allowed?: boolean;
}
interface BridgeSnapshot {
  ok: boolean;
  status: BridgeStatus;
  enabled: boolean;
  error?: string;
  hint?: string;
  serverName?: string;
  serverVersion?: string;
  tools?: BridgeToolInfo[];
  checkedAt: number;
}
interface BridgeCallResult {
  isError?: boolean;
  content?: Array<Record<string, unknown>>;
  structuredContent?: Record<string, unknown>;
  resolvedTool?: string;
}

class WindowsBridge {
  private readonly config = bridgeConfigFromEnv();
  private readonly timeoutMs = bridgeCallTimeoutMs();
  private client: Client | undefined;
  private snapshot: BridgeSnapshot | undefined;
  private statusFlight: Promise<BridgeSnapshot> | undefined;
  private inventoryFlight: Promise<BridgeSnapshot> | undefined;
  readonly profile = bridgeProfileFromEnv();
  readonly allowlist = profileAllowlist(this.profile);
  allowed(name: string): boolean { return this.profile === "system_full" || this.allowlist.has(name); }

  private failed(error: unknown): BridgeSnapshot {
    let message = error instanceof Error ? error.message : String(error);
    if (this.config.url) message = message.split(this.config.url).join("[local Windows bridge]");
    if (this.config.token) message = message.split(this.config.token).join("[REDACTED]");
    return { ok: false, enabled: this.config.enabled, status: "unavailable", error: redactSensitiveText(message),
      hint: "Windows control is enabled, but its local service could not be reached. Check this project's Windows bridge process/log; do not restart unrelated projects.", checkedAt: Date.now() };
  }
  private async disconnect(client: Client): Promise<void> {
    if (this.client === client) this.client = undefined;
    try { await client.close(); } catch { /* Keep the primary connection/tool error. */ }
  }
  async status(): Promise<BridgeSnapshot> {
    if (this.config.error) return { ok: false, enabled: this.config.enabled, status: "configuration_error", error: this.config.error, checkedAt: Date.now() };
    if (!this.config.enabled) return { ok: false, enabled: false, status: "disabled", error: "Windows control is not enabled for this project.", hint: "Enable Windows control for this project in DevBridge only when requested, then restart this project. No bridge connection was attempted.", checkedAt: Date.now() };
    if (this.statusFlight) return this.statusFlight;
    if (this.client && this.snapshot?.ok && Date.now() - this.snapshot.checkedAt <= SNAPSHOT_TTL_MS) return this.snapshot;
    const work = this.refreshStatus();
    this.statusFlight = work;
    try { return await work; } finally { if (this.statusFlight === work) this.statusFlight = undefined; }
  }
  private async refreshStatus(): Promise<BridgeSnapshot> {
    let client = this.client;
    try {
      if (client) {
        await client.ping({ timeout: Math.min(this.timeoutMs, 10_000) });
      } else {
        const headers = { accept: "application/json, text/event-stream", authorization: `Bearer ${this.config.token}` };
        const transport = new StreamableHTTPClientTransport(new URL(this.config.url!), {
          // Never bind an AbortSignal to the lifetime of a reusable transport.
          // Redirects are forbidden so a loopback service cannot forward the credential remotely.
          requestInit: { headers, redirect: "error" }
        });
        client = new Client({ name: "codexpro-windows-bridge", version: "0.29.0-localdev" }, { capabilities: {}, listMaxPages: 8 });
        this.client = client;
        const owner = client;
        client.onclose = () => { if (this.client === owner) { this.client = undefined; this.snapshot = undefined; } };
        await client.connect(transport, { timeout: Math.min(this.timeoutMs, 10_000) });
      }
      if (this.client !== client) throw new Error("Windows bridge closed during its health check.");
      const version = client.getServerVersion();
      this.snapshot = { ok: true, enabled: true, status: "connected", serverName: version?.name ?? "Windows-MCP", serverVersion: version?.version ?? "unknown", tools: this.snapshot?.tools, checkedAt: Date.now() };
      return this.snapshot;
    } catch (error) {
      if (client) await this.disconnect(client);
      this.snapshot = this.failed(error);
      return this.snapshot;
    }
  }
  async listTools(): Promise<BridgeSnapshot> {
    if (this.inventoryFlight) return this.inventoryFlight;
    const work = this.refreshInventory();
    this.inventoryFlight = work;
    try { return await work; } finally { if (this.inventoryFlight === work) this.inventoryFlight = undefined; }
  }
  private async refreshInventory(): Promise<BridgeSnapshot> {
    const snapshot = await this.status();
    const client = this.client;
    if (!snapshot.ok || !client) return snapshot;
    try {
      // SDK v2 walks bounded pages. Refresh prevents stale SDK cache from authorizing a removed tool.
      const result = await client.listTools({}, { timeout: Math.min(this.timeoutMs, 10_000), cacheMode: "refresh" });
      if (result.tools.length > 128 || Buffer.byteLength(JSON.stringify(result.tools), "utf8") > INVENTORY_MAX_BYTES) throw new Error("Windows tool inventory exceeds its 128-tool / 512 KiB safety budget.");
      const names = new Set<string>();
      const tools: BridgeToolInfo[] = result.tools.map(tool => {
        if (!tool.name || tool.name.length > 160 || names.has(tool.name)) throw new Error("Windows bridge returned an invalid or duplicate tool name.");
        names.add(tool.name);
        return { name: tool.name, title: tool.title ?? tool.name, description: tool.description?.slice(0, 4_000), inputSchema: tool.inputSchema as Record<string, unknown>, call_allowed: this.allowed(tool.name) };
      });
      if (this.client !== client) throw new Error("Windows bridge changed during inventory discovery.");
      this.snapshot = { ...snapshot, tools };
      return this.snapshot;
    } catch (error) {
      await this.disconnect(client);
      this.snapshot = this.failed(error);
      return this.snapshot;
    }
  }
  async callTool(name: string, args: Record<string, unknown> = {}): Promise<BridgeCallResult> {
    const requested = name.trim();
    if (!requested) throw new Error("windows_call requires a non-empty tool name.");
    const snapshot = await this.listTools();
    const client = this.client;
    if (!snapshot.ok || !client || !snapshot.tools) throw new Error(`${snapshot.status}: ${snapshot.error ?? "Windows bridge connection failed"} ${snapshot.hint ?? ""}`);
    const resolved = resolveWindowsCall(requested, args, snapshot.tools);
    if (!this.allowed(requested) || !this.allowed(resolved.name)) throw new Error(`Tool "${requested}" (native "${resolved.name}") is not allowed by windows profile "${this.profile}". No action was sent. System-level tools require complete access; inspect windows_list_tools for allowed UI tools.`);
    let result: BridgeCallResult;
    try {
      // One attempt only: a transport failure after an input action is an unknown outcome, never a replay instruction.
      result = await client.callTool({ name: resolved.name, arguments: resolved.arguments }, { timeout: this.timeoutMs }) as unknown as BridgeCallResult;
    } catch (error) {
      await this.disconnect(client);
      throw new Error(`${this.failed(error).error}. The action was not replayed; verify desktop state before retrying.`);
    }
    if (Buffer.byteLength(JSON.stringify(result), "utf8") > RESULT_MAX_BYTES) throw new Error("Windows result exceeds 6 MiB. The action was not replayed; use a smaller display/snapshot before retrying.");
    if (resolved.search && !result.isError) {
      const text = (result.content ?? []).filter(b => b.type === "text" && typeof b.text === "string").map(b => b.text).join("\n");
      const matches = searchSnapshotWindows(text, resolved.search);
      return { content: [{ type: "text", text: JSON.stringify(matches) }], structuredContent: matches, resolvedTool: resolved.name };
    }
    return { ...result, resolvedTool: resolved.name };
  }
}

function bridgeTitle(snapshot: BridgeSnapshot): string {
  return snapshot.ok ? `${snapshot.serverName ?? "Windows-MCP"} ${snapshot.serverVersion ?? "unknown"}` : snapshot.status;
}
function bridgeTextResult(text: string, structured: Record<string, unknown>): any {
  return { content: [{ type: "text", text: redactSensitiveText(text) }], structuredContent: redactStructured(structured) };
}
function bridgeErrorResult(error: unknown): any {
  const message = redactSensitiveText(error instanceof Error ? error.message : String(error));
  return { isError: true, content: [{ type: "text", text: message }], structuredContent: { error: message } };
}
function tagBridgeResult(result: any, name: string, title: string): any {
  if (!result || typeof result !== "object") return result;
  const structured = result.structuredContent;
  result.structuredContent = { codexpro_tool: name, codexpro_title: title, ...(structured && typeof structured === "object" && !Array.isArray(structured) ? structured : {}) };
  return result;
}
const WindowsCallArgumentsSchema = z.object({
  tool: z.string().min(1).max(160), arguments: z.record(z.string(), z.any()).optional()
}).strict();
interface BridgeDescriptor {
  name: string; title: string; description: string; inputSchema: Record<string, z.ZodTypeAny>;
  annotations: Record<string, boolean>; handler: (args: any) => any;
}
export function registerWindowsBridgeTools(server: McpServer, config: CodexProConfig): void {
  let bridge: WindowsBridge | undefined;
  const getBridge = (): WindowsBridge => bridge ??= new WindowsBridge();
  const s = server as any;
  const register = (descriptor: BridgeDescriptor): void => {
    const wrapped = async (args: any) => {
      try { return tagBridgeResult(await descriptor.handler(args ?? {}), descriptor.name, descriptor.title); }
      catch (error) { return tagBridgeResult(bridgeErrorResult(error), descriptor.name, descriptor.title); }
    };
    if (typeof s.registerTool !== "function") throw new Error("Unsupported MCP SDK: McpServer.registerTool is unavailable.");
    s.registerTool(descriptor.name, { title: descriptor.title, description: descriptor.description, inputSchema: z.object(descriptor.inputSchema), annotations: descriptor.annotations }, wrapped);
  };
  register({
    name: "windows_backend_status", title: "Windows Backend Status",
    description: "Check this project's Windows control bridge: disabled, configuration error, unreachable service, or connected. Never reveals credentials or URLs.",
    inputSchema: {}, annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: false },
    handler: async () => {
      const bridge = getBridge(), snapshot = await bridge.status();
      return bridgeTextResult(["# Windows Backend Status", `Status: ${snapshot.status}`, `Server: ${bridgeTitle(snapshot)}`, `Permission profile: ${bridge.profile}`, snapshot.error ?? "", snapshot.hint ?? ""].filter(Boolean).join("\n"), {
        ok: snapshot.ok, reachable: snapshot.ok, enabled: snapshot.enabled, status: snapshot.status,
        server_name: snapshot.serverName ?? null, server_version: snapshot.serverVersion ?? null,
        error: snapshot.error ?? null, recovery_hint: snapshot.hint ?? null, tools_count: snapshot.tools?.length ?? null,
        checked_at: snapshot.checkedAt, profile: bridge.profile, bridge: { local_only: true, fixed_port: true, project_scoped: true }
      });
    }
  });
  register({
    name: "windows_list_tools", title: "Windows List Tools",
    description: "Discover native Windows tool names, input schemas, permission flags, and separate validated legacy adapters. Read-only; use these schemas instead of guessing arguments.",
    inputSchema: {}, annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: true },
    handler: async () => {
      const bridge = getBridge(), snapshot = await bridge.listTools();
      if (!snapshot.ok) throw new Error(`${snapshot.status}: ${snapshot.error} ${snapshot.hint ?? ""}`);
      const tools = snapshot.tools ?? [], aliases = compatibilityInventory(tools, name => bridge.allowed(name));
      return bridgeTextResult(["# Windows List Tools", `Server: ${bridgeTitle(snapshot)}`, `Native count: ${tools.length}`, ...tools.map(t => `${t.name}: ${t.call_allowed ? "allowed" : "not allowed"}; inputSchema=${JSON.stringify(t.inputSchema)}`), "Compatibility adapters (not native inventory):", JSON.stringify(aliases)].join("\n"), {
        server_name: snapshot.serverName ?? null, server_version: snapshot.serverVersion ?? null, tools,
        tool_count: tools.length, compatibility_aliases: aliases, profile: bridge.profile, bridge: { reachable: true, local_only: true }
      });
    }
  });
  if (!config.connectionTest) register({
    name: "windows_call", title: "Windows Call",
    description: "Invoke one native Windows tool or a discovered compatibility adapter. Both requested and resolved tool names must be allowed. Inspect windows_list_tools inputSchema first. Mutating calls are never replayed automatically.",
    inputSchema: {
      tool: z.string().min(1).max(160).describe("Native tool name or a compatibility name from windows_list_tools."),
      arguments: z.record(z.string(), z.any()).optional().describe("Exact native or adapter inputSchema arguments.")
    },
    annotations: { readOnlyHint: false, openWorldHint: true, destructiveHint: true, idempotentHint: false },
    handler: async (args: unknown) => {
      const { tool, arguments: toolArguments } = WindowsCallArgumentsSchema.parse(args);
      const result = await getBridge().callTool(tool, toolArguments ?? {});
      const content = (result.content ?? []).map(block => block.type === "text" && typeof block.text === "string" ? { ...block, text: redactSensitiveText(block.text) } : redactStructured(block));
      const text = content.filter((b: any) => b.type === "text" && typeof b.text === "string").map((b: any) => b.text).join("\n");
      // Preserve native image blocks and structured results; do not turn screenshots into empty text.
      return { isError: result.isError === true, content: content.length ? content : [{ type: "text", text: "(no native content)" }], structuredContent: redactStructured({ tool, native_tool: result.resolvedTool ?? tool, bridge_ok: !result.isError, text, result: result.structuredContent ?? null }) };
    }
  });
}
