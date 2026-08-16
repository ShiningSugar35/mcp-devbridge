import { z } from "zod";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CodexProConfig } from "./config.js";
import { redactSensitiveText, redactStructured } from "./redact.js";

/*
 * LocalDev MCP Bridge Windows bridge.
 *
 * Always-registered tools that forward to a local Windows-MCP instance
 * (run by the LocalDev MCP Bridge desktop app with uvx on 127.0.0.1 only).
 * The bridge address is a FIXED internal port; the app never writes or
 * passes URLs or ports to CodexPro. The bearer token travels in the process
 * environment only and is never echoed in tool output.
 *
 * Tools:
 *   windows_backend_status - connection + capability status of the bridge
 *   windows_list_tools     - inspected tool inventory of the bridge server
 *   windows_call           - forward one tool call to the bridge (destructive)
 */

export const WINDOWS_BRIDGE_TOOL_NAMES = [
  "windows_backend_status",
  "windows_list_tools",
  "windows_call"
] as const;

const DEFAULT_WINDOWS_BRIDGE_PORT = 28731;
const SNAPSHOT_TTL_MS = 10_000;

/**
 * Windows 桥接权限档位（由引擎注入 CODEXPRO_WINDOWS_PROFILE）：
 *   desktop_ui  - 只放行 UI 操作白名单（点击/输入/快照/应用等），系统级工具拒绝；
 *   system_full - 放行桥端 inventory 中的全部工具（对应「完全访问」权限模式）。
 * 所有档位都要求工具同时出现在桥端实时 inventory 中。
 */
type WindowsProfile = "desktop_ui" | "system_full";

const DESKTOP_UI_ALLOWLIST = new Set([
  "Click",
  "DoubleClick",
  "Type",
  "HotKey",
  "MouseMove",
  "MouseScroll",
  "ScrollScreen",
  "App",
  "Snapshot",
  "Wait",
  "GetScreenSize",
  "CurrentCursorPosition",
  "SearchWindow",
  "List"
]);

function bridgeProfileFromEnv(): WindowsProfile {
  const raw = (process.env.CODEXPRO_WINDOWS_PROFILE ?? "desktop_ui").trim().toLowerCase();
  return raw === "system_full" ? "system_full" : "desktop_ui";
}

function profileAllowlist(profile: WindowsProfile): Set<string> {
  if (profile === "system_full") return new Set<string>();
  const allow = new Set(DESKTOP_UI_ALLOWLIST);
  const extra = (process.env.CODEXPRO_WINDOWS_DESKTOP_ALLOW ?? "")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  extra.forEach((name) => allow.add(name));
  return allow;
}

function bridgeUrlFromEnv(): string {
  const rawUrl = (process.env.CODEXPRO_WINDOWS_BRIDGE_URL ?? "").trim();
  if (rawUrl) {
    const normalized = rawUrl.replace(/\/+$/, "");
    if (!/^https?:\/\//.test(normalized)) {
      throw new Error("CODEXPRO_WINDOWS_BRIDGE_URL must be an http(s) URL.");
    }
    return normalized;
  }
  return `http://127.0.0.1:${DEFAULT_WINDOWS_BRIDGE_PORT}/mcp`;
}

function bridgeTokenFromEnv(): string {
  return process.env.CODEXPRO_WINDOWS_BRIDGE_TOKEN ?? "";
}

function bridgeCallTimeoutMs(): number {
  const raw = Number(process.env.CODEXPRO_WINDOWS_CALL_TIMEOUT_MS ?? 120_000);
  if (!Number.isFinite(raw)) return 120_000;
  return Math.max(1_000, Math.min(300_000, Math.floor(raw)));
}

interface BridgeToolInfo {
  name: string;
  title?: string;
  description?: string;
}

interface BridgeSnapshot {
  ok: boolean;
  error?: string;
  serverName?: string;
  serverVersion?: string;
  tools?: BridgeToolInfo[];
  checkedAt: number;
}

function failSnapshot(error: unknown): BridgeSnapshot {
  return {
    ok: false,
    error: redactSensitiveText(error instanceof Error ? error.message : String(error)),
    checkedAt: Date.now()
  };
}

interface BridgeCallResult {
  isError?: boolean;
  content?: Array<{ type?: string; text?: string }>;
}

class WindowsBridge {
  private readonly url: string;
  private readonly token: string;
  private readonly timeoutMs: number;
  private client: Client | undefined;
  private snapshot: BridgeSnapshot | undefined;
  readonly profile: WindowsProfile;
  readonly allowlist: Set<string>;

  constructor(url: string, token: string, timeoutMs: number) {
    this.url = url;
    this.token = token;
    this.timeoutMs = timeoutMs;
    this.profile = bridgeProfileFromEnv();
    this.allowlist = profileAllowlist(this.profile);
    if (this.token && Buffer.byteLength(this.token, "utf8") < 24) {
      throw new Error("CODEXPRO_WINDOWS_BRIDGE_TOKEN must be at least 24 bytes.");
    }
  }

  async status(): Promise<BridgeSnapshot> {
    const cached = this.snapshot;
    if (cached && cached.ok && this.client && Date.now() - cached.checkedAt <= SNAPSHOT_TTL_MS) {
      return cached;
    }

    this.client = undefined;
    const headers: Record<string, string> = { accept: "application/json, text/event-stream" };
    if (this.token) headers.authorization = `Bearer ${this.token}`;
    const transport = new StreamableHTTPClientTransport(new URL(this.url), {
      requestInit: { headers, signal: AbortSignal.timeout(this.timeoutMs) }
    });
    const client = new Client(
      { name: "codexpro-windows-bridge", version: "0.29.0-localdev" },
      { capabilities: {} }
    );
    try {
      await client.connect(transport);
      const serverVersion = client.getServerVersion();
      this.client = client;
      this.snapshot = {
        ok: true,
        serverName: serverVersion?.name ?? "Windows-MCP",
        serverVersion: serverVersion?.version ?? "unknown",
        checkedAt: Date.now()
      };
      return this.snapshot;
    } catch (error) {
      this.snapshot = failSnapshot(error);
      try {
        await client.close();
      } catch {
        // ignore secondary close failures
      }
      return this.snapshot;
    }
  }

  async listTools(): Promise<BridgeSnapshot> {
    const snapshot = await this.status();
    if (!snapshot.ok || !this.client) return snapshot;
    try {
      const result = await this.client.listTools({});
      const tools: BridgeToolInfo[] = (result.tools ?? []).map((tool) => ({
        name: tool.name,
        title: (tool as { title?: string }).title ?? tool.name,
        description: typeof tool.description === "string" ? tool.description.slice(0, 4_000) : undefined
      }));
      this.snapshot = { ...snapshot, tools };
      return this.snapshot;
    } catch (error) {
      return failSnapshot(error);
    }
  }

  async callTool(name: string, args: Record<string, unknown> = {}): Promise<BridgeCallResult> {
    if (!name || !name.trim()) throw new Error("windows_call requires a non-empty tool name.");

    // Allowlist（profile ∩ 实时 inventory）：不满足直接拒绝，绝不转发。
    const snapshot = await this.listTools();
    if (!snapshot.ok || !this.client || !snapshot.tools) {
      throw new Error(`Windows bridge unavailable: ${snapshot.error ?? "connection failed"}`);
    }
    const inventory = new Set(snapshot.tools.map((tool) => tool.name));
    const toolName = name.trim();
    if (!inventory.has(toolName)) {
      throw new Error(
        `tool "${toolName}" is not present in the Windows bridge inventory; refusing to call.`
      );
    }
    if (this.profile === "desktop_ui" && !this.allowlist.has(toolName)) {
      throw new Error(
        `tool "${toolName}" is not allowed by the current windows profile "desktop_ui". ` +
          `Allowed: ${[...this.allowlist].join(", ")}. ` +
          `System-level tools require the "complete access" permission mode (profile system_full).`
      );
    }

    const result = await this.client.callTool({ name: toolName, arguments: args });
    if (result.isError) throw new Error(`Windows bridge tool failed: ${name}`);
    return result as unknown as BridgeCallResult;
  }
}

function bridgeTitle(snapshot: BridgeSnapshot): string {
  if (!snapshot.ok) return "unavailable";
  return `${snapshot.serverName ?? "Windows-MCP"} ${snapshot.serverVersion ?? "unknown"}`.trim();
}

function renderToolList(tools: BridgeToolInfo[] | undefined): string {
  if (!tools || !tools.length) return "- No tools reported by the bridge.";
  return tools
    .map((tool) => `- ${tool.title ?? tool.name}${tool.description ? `: ${tool.description.replace(/\s+/g, " ").trim()}` : ""}`)
    .join("\n");
}

function bridgeTextResult(text: string, structured: Record<string, unknown>): any {
  return {
    content: [{ type: "text", text: redactSensitiveText(text) }],
    structuredContent: redactStructured(structured)
  };
}

function bridgeErrorResult(error: unknown): any {
  const message = redactSensitiveText(error instanceof Error ? error.message : String(error));
  return { isError: true, content: [{ type: "text", text: message }], structuredContent: { error: message } };
}

function tagBridgeResult(result: any, name: string, title: string): any {
  if (!result || typeof result !== "object") return result;
  const structured = result.structuredContent;
  const base = structured && typeof structured === "object" && !Array.isArray(structured) ? structured : {};
  result.structuredContent = { codexpro_tool: name, codexpro_title: title, ...base };
  return result;
}

const WindowsCallArgumentsSchema = z
  .object({
    tool: z.string().min(1).max(160).describe("Windows-MCP tool name to invoke, for example Click, App, PowerShell, FileSystem."),
    arguments: z.record(z.any()).optional().describe("Tool arguments accepted by the Windows-MCP tool.")
  })
  .strict();

type WindowsCallArguments = z.infer<typeof WindowsCallArgumentsSchema>;

interface BridgeDescriptor {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, z.ZodTypeAny>;
  annotations: Record<string, boolean>;
  handler: (args: any) => any;
}

export function registerWindowsBridgeTools(server: McpServer, _config: CodexProConfig): void {
  let bridge: WindowsBridge | undefined;
  const getBridge = (): WindowsBridge => {
    bridge ??= new WindowsBridge(bridgeUrlFromEnv(), bridgeTokenFromEnv(), bridgeCallTimeoutMs());
    return bridge;
  };

  const s = server as any;
  const register = (descriptor: BridgeDescriptor): void => {
    const wrapped = async (args: any) => {
      try {
        return tagBridgeResult(await descriptor.handler(args ?? {}), descriptor.name, descriptor.title);
      } catch (error) {
        return tagBridgeResult(bridgeErrorResult(error), descriptor.name, descriptor.title);
      }
    };
    if (typeof s.registerTool === "function") {
      s.registerTool(
        descriptor.name,
        {
          title: descriptor.title,
          description: descriptor.description,
          inputSchema: descriptor.inputSchema,
          annotations: descriptor.annotations
        },
        wrapped
      );
    } else if (typeof s.tool === "function") {
      s.tool(descriptor.name, descriptor.description, descriptor.inputSchema, wrapped);
    } else {
      throw new Error("Unsupported MCP SDK: McpServer has neither registerTool nor tool.");
    }
  };

  register({
    name: "windows_backend_status",
    title: "Windows Backend Status",
    description:
      "Check the local Windows-MCP bridge on its fixed internal port and report reachability, server name, version, active permission profile, and cached tool inventory. Never reveals bearer tokens or URLs.",
    inputSchema: {},
    annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: false },
    handler: async () => {
      const bridge = getBridge();
      const snapshot = await bridge.status();
      const lines = [
        "# Windows Backend Status",
        "",
        `Status: ${snapshot.ok ? "connected" : "unavailable"}`,
        `Server: ${bridgeTitle(snapshot)}`,
        `Permission profile: ${bridge.profile}`,
        ...(snapshot.error ? [`Error: ${snapshot.error}`] : []),
        `Tool inventory: ${snapshot.tools?.length ?? "cached later"}`,
        "",
        "The Windows bridge only listens on 127.0.0.1 at a fixed internal port; no public URL exists."
      ].join("\n");
      return bridgeTextResult(lines, {
        ok: snapshot.ok,
        reachable: snapshot.ok,
        server_name: snapshot.serverName ?? null,
        server_version: snapshot.serverVersion ?? null,
        error: snapshot.error ?? null,
        tools_count: snapshot.tools?.length ?? null,
        checked_at: snapshot.checkedAt,
        profile: bridge.profile,
        bridge: { local_only: true, fixed_port: true }
      });
    }
  });

  register({
    name: "windows_list_tools",
    title: "Windows List Tools",
    description:
      "List the tool inventory exposed by the local Windows bridge, with names and short descriptions. Read-only; no state is changed.",
    inputSchema: {},
    annotations: { readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: true },
    handler: async () => {
      const snapshot = await getBridge().listTools();
      if (!snapshot.ok) throw new Error(`Windows bridge unavailable: ${snapshot.error ?? "connection failed"}`);
      const tools = snapshot.tools ?? [];
      const lines = [
        "# Windows List Tools",
        "",
        `Server: ${bridgeTitle(snapshot)}`,
        `Count: ${tools.length}`,
        "",
        renderToolList(tools)
      ].join("\n");
      return bridgeTextResult(lines, {
        server_name: snapshot.serverName ?? null,
        server_version: snapshot.serverVersion ?? null,
        tools,
        tool_count: tools.length,
        bridge: { reachable: true, local_only: true }
      });
    }
  });

  if (!_config.connectionTest) register({
    name: "windows_call",
    title: "Windows Call",
    description:
      "Invoke one tool on the local Windows bridge, for example Click, App, Snapshot, or SearchWindow. Only tools allowed by the active permission profile (desktop_ui allowlist, or all inventory tools under system_full) can be called; anything else is refused before reaching the bridge. Use with explicit user instruction.",
    inputSchema: {
      tool: z.string().min(1).max(160).describe("Windows-MCP tool name to invoke, for example Click, App, Snapshot, SearchWindow."),
      arguments: z.record(z.any()).optional().describe("Tool arguments accepted by the Windows-MCP tool.")
    },
    annotations: { readOnlyHint: false, openWorldHint: true, destructiveHint: true, idempotentHint: false },
    handler: async (args: WindowsCallArguments) => {
      const parsed = WindowsCallArgumentsSchema.safeParse(args ?? {});
      if (!parsed.success) {
        throw new Error(parsed.error.issues.map((issue) => `${issue.path.join(".") || "arguments"}: ${issue.message}`).join("; "));
      }
      const { tool, arguments: toolArguments } = parsed.data;
      const result = await getBridge().callTool(tool, toolArguments ?? {});
      const content = Array.isArray(result.content) ? result.content : [];
      const text = content
        .map((block: any) => (typeof block?.text === "string" ? block.text : null))
        .filter((part: string | null): part is string => Boolean(part))
        .join("\n");
      const lines = ["# Windows Bridge Call", "", `Tool: ${tool}`, "Status: ok", "", text || "(no text output)"].join("\n");
      return bridgeTextResult(lines, {
        tool,
        bridge_ok: true,
        text
      });
    }
  });
}