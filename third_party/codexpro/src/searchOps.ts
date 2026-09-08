import fsp from "node:fs/promises";
import path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { StringDecoder } from "node:string_decoder";
import type { CodexProConfig } from "./config.js";
import type { Workspace } from "./guard.js";
import { CodexProError, PathGuard } from "./guard.js";
import { listFiles, textScanByteLimit } from "./fsOps.js";
import { redactSensitiveText } from "./redact.js";
import { searchWorkspaceStructured, type AnalysisSearchIntent, type StructuredSearchResult } from "./analysis/index.js";

export interface SearchOptions {
  query: string;
  regex: boolean;
  root?: string;
  glob?: string;
  includeHidden: boolean;
  maxResults: number;
  intent?: AnalysisSearchIntent;
  symbol?: string;
  includeTests?: boolean;
  /** Internal execution controls, not additions to the public MCP input schema. */
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface SearchResult {
  text: string;
  matches: Array<{ path: string; line: number; text: string }>;
  truncated: boolean;
  used: "ripgrep" | "node";
  warnings?: string[];
  analysis?: StructuredSearchResult;
}

const SEARCH_DEADLINE_MS = 20_000;
const SEARCH_INFLIGHT_LIMIT = 8;
let activeSearches = 0;

/** rg/where are direct leaf children. Keep admission until their actual close. */
function bindChildAbort(child: ChildProcess, signal: AbortSignal): void {
  let escalation: ReturnType<typeof setTimeout> | undefined;
  const abort = () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    child.kill("SIGTERM");
    escalation = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }, 500);
    escalation.unref?.();
  };
  signal.addEventListener("abort", abort, { once: true });
  child.once("close", () => {
    if (escalation) clearTimeout(escalation);
    signal.removeEventListener("abort", abort);
  });
  if (signal.aborted) abort();
}

function commandExists(command: string, signal: AbortSignal): Promise<boolean> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = process.platform === "win32"
      ? spawn("where", [command], { stdio: "ignore", shell: false, windowsHide: true })
      : spawn("/bin/sh", ["-lc", `command -v ${command} >/dev/null 2>&1`], { stdio: "ignore" });
    bindChildAbort(child, signal);
    let failed = false;
    child.on("error", () => { failed = true; });
    child.on("close", (code) => signal.aborted ? reject(signal.reason) : resolve(!failed && code === 0));
  });
}

function truncateLine(line: string, max = 400): string {
  return line.length <= max ? line : `${line.slice(0, max)}…`;
}

async function runRipgrep(config: CodexProConfig, guard: PathGuard, workspace: Workspace,
  options: SearchOptions, signal: AbortSignal, result: SearchResult): Promise<void> {
  signal.throwIfAborted();
  result.used = "ripgrep";
  const target = guard.resolve(workspace, options.root ?? ".");
  const args = ["--json", "--line-number", "--with-filename", "--no-heading", "--color=never", "--max-columns", "500", "--max-count", "50", "--max-filesize", String(textScanByteLimit(config))];
  if (!options.regex) args.push("--fixed-strings");
  if (options.includeHidden) args.push("--hidden");
  for (const glob of config.blockedGlobs) args.push("-g", `!${glob}`);
  if (options.glob) args.push("-g", options.glob);
  args.push("-e", options.query, "--", target.absPath);

  await new Promise<void>((resolve, reject) => {
    const child = spawn("rg", args, { cwd: workspace.root, env: { ...process.env, NO_COLOR: "1" }, windowsHide: true });
    bindChildAbort(child, signal);
    const decoder = new StringDecoder("utf8");
    let pending = "";
    let stderr = "";
    let outputBytes = 0;
    let limited = false;
    let failure: Error | undefined;
    const stop = () => {
      if (!limited) {
        limited = true;
        result.truncated = true;
        child.kill("SIGTERM");
      }
    };
    const consume = (line: string) => {
      if (!line || signal.aborted || failure) return;
      try {
        const value = JSON.parse(line);
        if (value.type !== "match") return;
        const abs = path.resolve(value.data?.path?.text ?? "");
        const rel = path.relative(workspace.root, abs).split(path.sep).join("/");
        if (rel.startsWith("..") || guard.isBlockedRelativePath(rel)) return;
        if (result.matches.length >= options.maxResults) { stop(); return; }
        const text = String(value.data?.lines?.text ?? "").replace(/\r?\n$/, "");
        result.matches.push({ path: rel || ".", line: Number(value.data?.line_number ?? 0), text: redactSensitiveText(truncateLine(text)) });
      } catch (error) {
        failure = new CodexProError(`ripgrep returned malformed JSON: ${redactSensitiveText(error instanceof Error ? error.message : String(error))}`);
        child.kill("SIGTERM");
      }
    };
    child.stdout.on("data", (chunk: Buffer) => {
      if (limited || signal.aborted || failure) return;
      const retained = chunk.subarray(0, Math.max(0, config.maxOutputBytes - outputBytes));
      outputBytes += retained.length;
      pending += decoder.write(retained);
      let newline: number;
      while ((newline = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, newline);
        pending = pending.slice(newline + 1);
        consume(line);
      }
      if (retained.length < chunk.length) stop();
    });
    child.stderr.on("data", (chunk: Buffer) => {
      if (stderr.length < 8192) stderr = (stderr + chunk.toString("utf8")).slice(0, 8192);
    });
    child.on("error", (error) => { failure = error; });
    child.on("close", (code) => {
      if (!limited && !signal.aborted && !failure) consume(pending + decoder.end());
      if (signal.aborted) { reject(signal.reason); return; }
      if (failure) { reject(failure); return; }
      if (code && code > 1 && !limited) {
        reject(new CodexProError(redactSensitiveText(stderr.trim()) || `ripgrep failed with exit code ${code}`));
        return;
      }
      resolve();
    });
  });
}

async function runNodeSearch(config: CodexProConfig, guard: PathGuard, workspace: Workspace,
  options: SearchOptions, signal: AbortSignal, result: SearchResult): Promise<void> {
  if (options.regex) throw new CodexProError("regex search requires ripgrep. Install rg or retry with regex=false.");
  const scanBytes = textScanByteLimit(config);
  let visibleMatches = 0;
  const files = await listFiles(guard, workspace, {
    root: options.root, glob: options.glob, includeHidden: options.includeHidden,
    maxFiles: 20_000, signal,
    onFile: async (rel) => {
      signal.throwIfAborted();
      const resolved = guard.resolve(workspace, rel);
      try {
        const stat = await fsp.stat(resolved.absPath);
        signal.throwIfAborted();
        if (stat.size > scanBytes) return true;
        const buffer = await fsp.readFile(resolved.absPath, { signal });
        signal.throwIfAborted();
        if (buffer.includes(0)) return true;
        const lines = buffer.toString("utf8").split(/\r?\n/);
        for (let i = 0; i < lines.length; i += 1) {
          if ((i & 0xff) === 0) signal.throwIfAborted();
          if (!lines[i].includes(options.query)) continue;
          visibleMatches += 1;
          if (result.matches.length >= options.maxResults) { result.truncated = true; return false; }
          result.matches.push({ path: rel, line: i + 1, text: redactSensitiveText(truncateLine(lines[i])) });
        }
      } catch {
        signal.throwIfAborted();
        // Skip unreadable files, but never swallow cancellation.
      }
      return visibleMatches <= options.maxResults;
    }
  });
  if (files.length >= 20_000) {
    result.truncated = true;
    result.warnings?.push("Node search reached the 20000-file enumeration limit; narrow path/glob for complete coverage.");
  }
}

function unavailableAnalysis(options: SearchOptions, warning: string, key = "unavailable"): StructuredSearchResult {
  return {
    schemaVersion: 1, query: options.query,
    intent: options.intent && options.intent !== "auto" ? options.intent : "text",
    groups: { definitions: [], references: [], tests: [], configuration: [], documentation: [], other: [] },
    matches: [],
    coverage: { inventoryFiles: 0, analyzedFiles: 0, scannedBytes: 0, symbolCount: 0, relationshipCount: 0, truncated: true, warnings: [warning] },
    warnings: [warning], cache: { hit: false, key }
  };
}

export async function searchWorkspace(config: CodexProConfig, guard: PathGuard, workspace: Workspace,
  rawOptions: Partial<SearchOptions>): Promise<SearchResult> {
  rawOptions.signal?.throwIfAborted();
  const query = rawOptions.symbol?.toString() || rawOptions.query?.toString() || "";
  if (!query) throw new CodexProError("query is required.");
  guard.resolve(workspace, rawOptions.root ?? ".");
  if (activeSearches >= SEARCH_INFLIGHT_LIMIT) throw new CodexProError("Search is busy (8 active operations); no additional scan was started.");
  const options: SearchOptions = {
    query, regex: Boolean(rawOptions.regex), root: rawOptions.root, glob: rawOptions.glob,
    includeHidden: Boolean(rawOptions.includeHidden),
    maxResults: Math.max(1, Math.min(rawOptions.maxResults ?? config.maxSearchResults, config.maxSearchResults)),
    intent: rawOptions.intent, symbol: rawOptions.symbol, includeTests: rawOptions.includeTests
  };
  const timeoutMs = Number.isFinite(rawOptions.timeoutMs) ? Math.max(1, Math.min(rawOptions.timeoutMs!, SEARCH_DEADLINE_MS)) : SEARCH_DEADLINE_MS;
  const controller = new AbortController();
  const signal = controller.signal;
  const forwardAbort = () => controller.abort(new Error("Search cancelled by caller."));
  rawOptions.signal?.addEventListener("abort", forwardAbort, { once: true });
  if (rawOptions.signal?.aborted) forwardAbort();
  const timer = setTimeout(() => controller.abort(new Error(`Search exceeded shared ${timeoutMs}ms deadline.`)), timeoutMs);
  const result: SearchResult = { text: "", matches: [], truncated: false, used: "node", warnings: [] };
  const structuredRequested = rawOptions.intent !== undefined || rawOptions.symbol !== undefined || rawOptions.includeTests !== undefined;
  let onAbort: () => void = () => {};
  const aborted = new Promise<void>((resolve) => { onAbort = resolve; signal.addEventListener("abort", onAbort, { once: true }); });
  activeSearches += 1;
  const work = (async () => {
    const hasRg = await commandExists("rg", signal);
    signal.throwIfAborted();
    if (!hasRg && options.regex) throw new CodexProError("regex search requires ripgrep. Install rg or retry with regex=false.");
    const lexical = hasRg ? runRipgrep(config, guard, workspace, options, signal, result) : runNodeSearch(config, guard, workspace, options, signal, result);
    const structured = (async () => {
      if (!structuredRequested) return;
      if (!config.analysisEnabled) {
        result.analysis = unavailableAnalysis(options, "Repository analysis is disabled by configuration.", "disabled");
        return;
      }
      try {
        result.analysis = await searchWorkspaceStructured(config, guard, workspace, {
          query, intent: options.intent ?? "auto", includeTests: Boolean(options.includeTests),
          regex: options.regex, root: options.root, maxResults: options.maxResults, signal, timeoutMs: Math.min(timeoutMs, 15_000)
        });
      } catch (error) {
        result.analysis = unavailableAnalysis(options, `Repository analysis unavailable: ${redactSensitiveText(error instanceof Error ? error.message : String(error))}`);
      }
    })();
    // Do not release capacity when only one branch ends or the HTTP waiter leaves.
    const settled = await Promise.allSettled([lexical, structured]);
    const failed = settled.find((entry) => entry.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
  })().finally(() => { activeSearches -= 1; });
  try {
    await Promise.race([work, aborted]);
    if (rawOptions.signal?.aborted) throw new CodexProError("Search cancelled by caller.");
    if (signal.aborted) {
      result.truncated = true;
      const warning = `Search exceeded shared ${timeoutMs}ms deadline; partial results only. Narrow path/glob or use a durable background search for exhaustive coverage.`;
      result.warnings?.push(warning);
      if (structuredRequested && !result.analysis) result.analysis = unavailableAnalysis(options, warning);
    }
    const snapshot = { ...result, matches: [...result.matches], warnings: [...(result.warnings ?? [])] };
    snapshot.text = snapshot.matches.map((m) => `${m.path}:${m.line}: ${m.text}`).join("\n") || (snapshot.truncated ? "Search incomplete; no matches found in the scanned portion." : "No matches.");
    if (snapshot.warnings.length) snapshot.text += "\n\nWarnings:\n" + snapshot.warnings.join("\n");
    return snapshot;
  } catch (error) {
    if (!signal.aborted || rawOptions.signal?.aborted) throw error;
    // Abort can reject work before the abort observer wins Promise.race.
    const warning = `Search exceeded shared ${timeoutMs}ms deadline; partial results only. Narrow path/glob for complete coverage.`;
    return { ...result, matches: [...result.matches], truncated: true, warnings: [warning],
      text: (result.matches.map((m) => `${m.path}:${m.line}: ${m.text}`).join("\n") || "Search incomplete; no matches found in the scanned portion.") + "\n\n" + warning,
      ...(structuredRequested && !result.analysis ? { analysis: unavailableAnalysis(options, warning) } : {}) };
  } finally {
    clearTimeout(timer);
    rawOptions.signal?.removeEventListener("abort", forwardAbort);
    signal.removeEventListener("abort", onAbort);
  }
}
