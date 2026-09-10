import { z } from "zod";

// Adapters for the published windows-mcp 0.8.2 contract, not name-only guesses.
// Native inventory names always win. Unrecognized arguments never reach the desktop.
const coordinate = z.number().int().min(-1_000_000).max(1_000_000);
const pointerFields = {
  x: coordinate.optional(), y: coordinate.optional(),
  loc: z.tuple([coordinate, coordinate]).optional(),
  label: z.number().int().nonnegative().optional()
};
const MoveSchema = z.object(pointerFields).strict();
const ClickSchema = z.object({ ...pointerFields, button: z.enum(["left", "right", "middle"]).default("left") }).strict();
const ScrollSchema = z.object({
  ...pointerFields,
  type: z.enum(["vertical", "horizontal"]).optional(),
  direction: z.enum(["up", "down", "left", "right"]),
  wheel_times: z.number().int().min(1).max(100).default(1)
}).strict();
const HotKeySchema = z.object({
  keys: z.union([z.array(z.string().trim().min(1).max(40)).min(1).max(8), z.string().trim().min(1).max(320)]).optional(),
  shortcut: z.string().trim().min(1).max(320).optional(),
  hotkey: z.string().trim().min(1).max(320).optional()
}).strict();
const SearchSchema = z.object({
  query: z.string().trim().min(1).max(500).optional(),
  name: z.string().trim().min(1).max(500).optional(),
  title: z.string().trim().min(1).max(500).optional(),
  exact: z.boolean().default(false),
  case_sensitive: z.boolean().default(false),
  limit: z.number().int().min(1).max(100).default(50)
}).strict();

type Pointer = z.infer<typeof MoveSchema>;
function pointer(value: Pointer, required: boolean): Record<string, unknown> {
  const hasXY = value.x !== undefined || value.y !== undefined;
  if (hasXY && (value.x === undefined || value.y === undefined)) throw new Error("Both x and y are required.");
  const count = Number(hasXY) + Number(value.loc !== undefined) + Number(value.label !== undefined);
  if (count > 1 || (required && count !== 1)) throw new Error("Provide exactly one of loc=[x,y], x/y, or label; coordinate forms cannot be combined.");
  if (hasXY) return { loc: [value.x, value.y] };
  if (value.loc !== undefined) return { loc: value.loc };
  if (value.label !== undefined) return { label: value.label };
  return {};
}

const DEFINITIONS = {
  MouseMove: { native_tool: "Move", schema: MoveSchema, read_only: false, note: "loc=[x,y], x/y, or label; always drag=false." },
  MouseScroll: { native_tool: "Scroll", schema: ScrollSchema, read_only: false, note: "Explicit direction and wheel_times; signed delta/amount are not guessed." },
  ScrollScreen: { native_tool: "Scroll", schema: ScrollSchema, read_only: false, note: "Explicit direction and wheel_times; optional coordinates." },
  HotKey: { native_tool: "Shortcut", schema: HotKeySchema, read_only: false, note: "Exactly one of keys, shortcut, hotkey; no conflicting combinations." },
  DoubleClick: { native_tool: "Click", schema: ClickSchema, read_only: false, note: "Exactly one pointer; clicks is fixed to 2." },
  SearchWindow: { native_tool: "Snapshot", schema: SearchSchema, read_only: true, note: "Exactly one of query/name/title. Readonly title search of opened windows; does not switch or launch. Supports the pinned 0.8.2 window table; unknown formats fail explicitly." }
} as const;

type AliasName = keyof typeof DEFINITIONS;
function isAlias(name: string): name is AliasName { return Object.hasOwn(DEFINITIONS, name); }
export interface CompatibleTool { name: string; inputSchema?: Record<string, unknown> }
export function compatibilityInventory(tools: CompatibleTool[], allowed: (name: string) => boolean): Record<string, unknown>[] {
  const names = new Set(tools.map(t => t.name));
  return Object.entries(DEFINITIONS).filter(([name]) => !names.has(name)).map(([name, definition]) => ({
    name, native_tool: definition.native_tool, inputSchema: z.toJSONSchema(definition.schema, { io: "input" }),
    read_only: definition.read_only, description: definition.note,
    call_allowed: names.has(definition.native_tool) && allowed(name) && allowed(definition.native_tool),
    native_inventory: false
  }));
}

export interface ResolvedWindowsCall { name: string; arguments: Record<string, unknown>; alias?: AliasName; search?: z.infer<typeof SearchSchema> }
export function resolveWindowsCall(name: string, args: Record<string, unknown>, tools: CompatibleTool[]): ResolvedWindowsCall {
  if (tools.some(t => t.name === name)) return { name, arguments: args };
  if (!isAlias(name)) throw new Error(`Tool "${name}" is not in the Windows bridge inventory. Call windows_list_tools for native inputSchema and supported compatibility aliases; no action was sent.`);
  const definition = DEFINITIONS[name];
  const target = tools.find(t => t.name === definition.native_tool);
  if (!target) throw new Error(`Compatibility target ${definition.native_tool} is unavailable for ${name}; no action was sent.`);
  let converted: Record<string, unknown>;
  let search: z.infer<typeof SearchSchema> | undefined;
  switch (name) {
    case "MouseMove": converted = { ...pointer(MoveSchema.parse(args), true), drag: false }; break;
    case "DoubleClick": {
      const parsed = ClickSchema.parse(args);
      converted = { ...pointer(parsed, true), button: parsed.button, clicks: 2 }; break;
    }
    case "MouseScroll": case "ScrollScreen": {
      const parsed = ScrollSchema.parse(args);
      const type = parsed.type ?? (["left", "right"].includes(parsed.direction) ? "horizontal" : "vertical");
      if ((type === "vertical") !== ["up", "down"].includes(parsed.direction)) throw new Error("Scroll type and direction disagree; no action was sent.");
      converted = { ...pointer(parsed, false), type, direction: parsed.direction, wheel_times: parsed.wheel_times }; break;
    }
    case "HotKey": {
      const parsed = HotKeySchema.parse(args);
      const supplied = [parsed.keys, parsed.shortcut, parsed.hotkey].filter(v => v !== undefined);
      if (supplied.length !== 1) throw new Error("Provide exactly one of keys, shortcut, or hotkey; no action was sent.");
      const shortcut = Array.isArray(supplied[0]) ? supplied[0].join("+") : supplied[0] as string;
      if (shortcut.split("+").some(k => !k.trim()) || /[\r\n\0]/.test(shortcut)) throw new Error("Invalid shortcut combination; no action was sent.");
      converted = { shortcut }; break;
    }
    case "SearchWindow": {
      search = SearchSchema.parse(args);
      if ([search.query, search.name, search.title].filter(v => v !== undefined).length !== 1) throw new Error("SearchWindow requires exactly one of query, name, or title; it never activates a window.");
      converted = { use_vision: false, use_ui_tree: false, use_dom: false }; break;
    }
  }
  // Fail on schema drift before forwarding an adapted action. Do not invent fields.
  const properties = target.inputSchema?.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties) || Object.keys(converted).some(key => !Object.hasOwn(properties, key))) {
    throw new Error(`Native ${definition.native_tool} schema no longer supports the ${name} adapter. Use windows_list_tools and the native name; no action was sent.`);
  }
  const required = target.inputSchema?.required;
  if (Array.isArray(required) && required.some(key => typeof key === "string" && !Object.hasOwn(converted, key))) throw new Error(`Native ${definition.native_tool} has additional required arguments; no action was sent.`);
  return { name: definition.native_tool, arguments: converted, alias: name, search };
}

interface WindowRow { name: string; depth: number; status: string; width: number; height: number; handle: string }
export function searchSnapshotWindows(text: string, query: NonNullable<ResolvedWindowsCall["search"]>): Record<string, unknown> {
  if (Buffer.byteLength(text, "utf8") > 1_048_576) throw new Error("Window snapshot exceeds the 1 MiB parsing budget. Use native Snapshot with a display filter.");
  // FastMCP may return a raw string or a JSON-encoded list of strings.
  if (text.trimStart().startsWith("[")) {
    try { const parsed: unknown = JSON.parse(text); if (Array.isArray(parsed) && parsed.every(v => typeof v === "string")) text = parsed.join("\n"); } catch { /* strict table validation below */ }
  }
  const section = /(?:^|\n)[\t ]*Opened Windows:[\t ]*\r?\n([\s\S]*?)(?=\r?\n[\t ]*UI Tree:|$)/.exec(text)?.[1];
  const formatError = () => new Error("SearchWindow could not recognize the pinned windows-mcp 0.8.2 opened-window table. Use native Snapshot; this is not evidence of zero matching windows.");
  if (section === undefined) throw formatError();
  const lines = section.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const windows: WindowRow[] = [];
  if (!(lines.length === 1 && lines[0] === "No windows found")) {
    if (lines.length < 3 || lines[0]?.split(/\s+/).join(" ") !== "Name Depth Status Width Height Handle" || !/^-+(?:\s+-+){5}$/.test(lines[1] ?? "")) throw formatError();
    if (lines.length > 1002) throw new Error("Window table exceeds the 1000-window parsing budget; no empty-result assertion was made.");
    for (const line of lines.slice(2)) {
      const row = /^(.+?)\s+(-?\d+)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s*$/.exec(line);
      if (!row) throw formatError();
      const depth = Number(row[2]), width = Number(row[4]), height = Number(row[5]);
      if (![depth, width, height].every(Number.isSafeInteger)) throw formatError();
      windows.push({ name: row[1]!.trimEnd(), depth, status: row[3]!, width, height, handle: row[6]! });
    }
  }
  const needle = query.query ?? query.name ?? query.title!;
  const normalize = (s: string) => query.case_sensitive ? s : s.toLowerCase();
  const matches = windows.filter(w => query.exact ? normalize(w.name) === normalize(needle) : normalize(w.name).includes(normalize(needle)));
  return { query: needle, matches: matches.slice(0, query.limit), match_count: matches.length, total_windows: windows.length, truncated: matches.length > query.limit, read_only: true, source: "Snapshot.Opened Windows", format_contract: "windows-mcp 0.8.2" };
}
