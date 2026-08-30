import type { CodexProConfig } from "../config.js";
import fsp from "node:fs/promises";
import type { PathGuard, Workspace } from "../guard.js";
import { redactSensitiveText } from "../redact.js";
import { detectProjectTypes } from "./classify.js";
import { getCachedWorkspaceAnalysis, invalidateWorkspaceAnalysis, setCachedWorkspaceAnalysis } from "./cache.js";
import { extractWorkspaceFiles } from "./extract.js";
import { buildRelationships } from "./graph.js";
import { inventoryWorkspace } from "./inventory.js";
import { classifySearchIntent, emptySearchGroups, groupForFile, sortStructuredMatches } from "./rank.js";
import type { AnalysisSearchIntent, StructuredSearchMatch, StructuredSearchResult, WorkspaceAnalysis } from "./types.js";

function cacheKey(workspace: Workspace, fingerprint: string, config: CodexProConfig, root: string): string {
  return `${workspace.id}:${root}:${fingerprint}:${JSON.stringify(config.analysisLimits)}`;
}

function areasFor(files: WorkspaceAnalysis["files"]): WorkspaceAnalysis["areas"] {
  const counts = new Map<string, { role: WorkspaceAnalysis["files"][number]["role"]; files: number }>();
  for (const file of files) {
    const top = file.path.includes("/") ? file.path.split("/")[0] : ".";
    const current = counts.get(top) ?? { role: file.role, files: 0 };
    current.files += 1;
    if (current.role === "other" && file.role !== "other") current.role = file.role;
    counts.set(top, current);
  }
  return [...counts.entries()].map(([areaPath, value]) => ({ path: areaPath, ...value })).sort((a, b) => b.files - a.files || a.path.localeCompare(b.path));
}

async function buildWorkspaceAnalysis(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  root = ".",
  signal?: AbortSignal
): Promise<WorkspaceAnalysis> {
  if (!config.analysisEnabled) throw new Error("Repository analysis is disabled by CODEXPRO_ANALYSIS=0.");
  signal?.throwIfAborted();
  const normalizedRoot = root.trim() || ".";
  const inventory = await inventoryWorkspace(config, guard, workspace, normalizedRoot, signal);
  signal?.throwIfAborted();
  const key = cacheKey(workspace, inventory.fingerprint, config, normalizedRoot);
  const cached = getCachedWorkspaceAnalysis(key);
  if (cached) return { ...cached, cache: { hit: true, key } };

  const extraction = await extractWorkspaceFiles(config, guard, workspace, inventory.files, signal);
  signal?.throwIfAborted();
  const symbols = extraction.files.flatMap((file) => file.symbols).slice(0, config.analysisLimits.maxSymbols);
  const relationships = buildRelationships(extraction.files, inventory.files, config.analysisLimits.maxRelationships);
  signal?.throwIfAborted();
  const languages = [...new Set(inventory.files.map((file) => file.language).filter((language) => language !== "unknown"))].sort();
  const warnings = [...inventory.coverage.warnings, ...extraction.warnings];
  const result: WorkspaceAnalysis = {
    schemaVersion: 1,
    workspaceId: workspace.id,
    root: workspace.root,
    languages,
    projectTypes: detectProjectTypes(inventory.files),
    entrypoints: inventory.files.filter((file) => file.entrypoint).map((file) => file.path),
    importantFiles: inventory.files.filter((file) => file.role === "config" || /(^|\/)(README|AGENTS)\.md$/i.test(file.path)).map((file) => file.path),
    areas: areasFor(inventory.files),
    files: inventory.files,
    symbols,
    relationships,
    coverage: {
      ...inventory.coverage,
      analyzedFiles: extraction.analyzedFiles,
      scannedBytes: extraction.scannedBytes,
      symbolCount: symbols.length,
      relationshipCount: relationships.length,
      truncated: inventory.coverage.truncated || extraction.truncated,
      warnings
    },
    warnings,
    fingerprint: inventory.fingerprint,
    cache: { hit: false, key }
  };
  setCachedWorkspaceAnalysis(key, result);
  return result;
}

export async function inspectWorkspace(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  root = ".",
  execution: AnalysisExecutionOptions = {}
): Promise<WorkspaceAnalysis> {
  if (!config.analysisEnabled) throw new Error("Repository analysis is disabled by CODEXPRO_ANALYSIS=0.");
  execution.signal?.throwIfAborted();
  const normalizedRoot = root.trim() || ".";
  const scopeKey = analysisScopeKey(workspace, config, normalizedRoot);
  let shared = analysisInflight.get(scopeKey);
  if (!shared) {
    if (analysisInflight.size >= MAX_ANALYSIS_INFLIGHT) {
      throw new Error(`Repository analysis is busy (${analysisInflight.size}/${MAX_ANALYSIS_INFLIGHT} active builds).`);
    }
    const controller = new AbortController();
    const buildDeadlineMs = DEFAULT_ANALYSIS_DEADLINE_MS;
    const timer = setTimeout(() => controller.abort(new Error(`Repository analysis exceeded ${buildDeadlineMs}ms deadline.`)), buildDeadlineMs);
    timer.unref?.();
    shared = buildWorkspaceAnalysis(config, guard, workspace, normalizedRoot, controller.signal).finally(() => clearTimeout(timer));
    analysisBuildsStarted += 1;
    analysisInflight.set(scopeKey, shared);
    void shared.finally(() => {
      if (analysisInflight.get(scopeKey) === shared) analysisInflight.delete(scopeKey);
    }).catch(() => undefined);
  }
  return awaitSharedAnalysis(shared, execution.signal, execution.timeoutMs);
}

export async function searchWorkspaceStructured(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  options: { query: string; intent?: AnalysisSearchIntent; includeTests?: boolean; regex?: boolean; root?: string; maxResults?: number; signal?: AbortSignal; timeoutMs?: number }
): Promise<StructuredSearchResult> {
  const query = options.query.trim();
  if (!query) throw new Error("query is required.");
  const searchDeadlineMs = boundedAnalysisDeadline(options.timeoutMs);
  const searchDeadlineAt = Date.now() + searchDeadlineMs;
  const assertSearchActive = () => {
    options.signal?.throwIfAborted();
    if (Date.now() >= searchDeadlineAt) {
      throw new Error(`Repository structured search exceeded ${searchDeadlineMs}ms deadline.`);
    }
  };
  assertSearchActive();
  const analysisRoot = options.includeTests ? "." : options.root?.trim() || ".";
  const analysis = await inspectWorkspace(config, guard, workspace, analysisRoot, { signal: options.signal, timeoutMs: options.timeoutMs });
  assertSearchActive();
  const intent = classifySearchIntent(query, options.intent ?? "auto", options.regex);
  const groups = emptySearchGroups();
  const lowered = query.toLowerCase();
  const matches: StructuredSearchMatch[] = [];
  const warnings = [...analysis.warnings];
  const resultLimit = Math.max(1, Math.min(options.maxResults ?? config.maxSearchResults, config.maxSearchResults));
  const candidateLimit = Math.max(resultLimit, Math.min(resultLimit * 4, 20_000));
  const resolvedRoot = options.root?.trim() ? guard.resolve(workspace, options.root).relPath.replace(/^\.\/?$/, "") : "";
  const inScope = (filePath: string) => !resolvedRoot || filePath === resolvedRoot || filePath.startsWith(`${resolvedRoot}/`);
  const definitionsByPath = new Map<string, Map<number, WorkspaceAnalysis["symbols"][number]>>();
  for (const symbol of analysis.symbols) {
    assertSearchActive();
    const byLine = definitionsByPath.get(symbol.path) ?? new Map<number, WorkspaceAnalysis["symbols"][number]>();
    byLine.set(symbol.line, symbol);
    definitionsByPath.set(symbol.path, byLine);
  }
  if (options.regex) {
    warnings.push("Grouped results are unavailable for regular expression searches. Lexical regex matching remains delegated to ripgrep.");
    return {
      schemaVersion: 1,
      query,
      intent,
      groups,
      matches: [],
      coverage: { ...analysis.coverage, truncated: true, warnings },
      warnings,
      cache: analysis.cache
    };
  }
  let scannedFiles = 0;
  let scannedBytes = 0;
  let searchBudgetReached = false;
  let candidateLimitReached = false;
  let skippedFiles = 0;

  scan:
  for (const file of analysis.files) {
    assertSearchActive();
    if (file.generated || (!options.includeTests && file.role === "test")) continue;
    if (!inScope(file.path) && !(options.includeTests && file.role === "test")) continue;
    if (scannedFiles >= config.analysisLimits.maxAnalyzedFiles || scannedBytes + file.bytes > config.analysisLimits.maxScannedBytes) {
      searchBudgetReached = true;
      break;
    }
    let text: string;
    try {
      const resolved = guard.resolve(workspace, file.path);
      text = await fsp.readFile(resolved.absPath, { encoding: "utf8", signal: options.signal });
    } catch {
      options.signal?.throwIfAborted();
      skippedFiles += 1;
      continue;
    }
    assertSearchActive();
    const actualBytes = Buffer.byteLength(text, "utf8");
    if (scannedBytes + actualBytes > config.analysisLimits.maxScannedBytes) {
      searchBudgetReached = true;
      break;
    }
    scannedFiles += 1;
    scannedBytes += actualBytes;
    const definitions = definitionsByPath.get(file.path) ?? new Map();
    const lines = text.split(/\r?\n/);
    for (let index = 0; index < lines.length; index += 1) {
      if ((index & 0xff) === 0) assertSearchActive();
      const line = lines[index];
      if (!line.toLowerCase().includes(lowered)) continue;
      const symbol = definitions.get(index + 1);
      const isDefinition = Boolean(symbol && symbol.name.toLowerCase() === lowered);
      const group = groupForFile(analysis, file.path, isDefinition);
      const reasons = isDefinition ? ["exact text match", "symbol definition"] : file.role === "test" ? ["exact text match", "related test"] : ["exact text match"];
      if (matches.length >= candidateLimit) {
        candidateLimitReached = true;
        break scan;
      }
      matches.push({
        path: file.path,
        line: index + 1,
        text: redactSensitiveText(line.trim().slice(0, 400)),
        group,
        score: isDefinition ? 190 : file.role === "test" ? 160 : 100,
        reasons,
        confidence: isDefinition ? "strong" : "exact",
        source: "built-in analysis"
      });
    }
  }

  if (searchBudgetReached) warnings.push("Grouped search reached its configured file or byte limit.");
  if (skippedFiles) warnings.push(`Grouped search skipped ${skippedFiles} file${skippedFiles === 1 ? "" : "s"} that changed or became unreadable during analysis.`);

  if (intent === "references" || intent === "impact") {
    const definitionPaths = new Set(
      analysis.symbols
        .filter((symbol) => symbol.name.toLowerCase() === lowered)
        .map((symbol) => symbol.path)
    );
    for (const relationship of analysis.relationships) {
      assertSearchActive();
      if (!definitionPaths.has(relationship.to)) continue;
      const group = relationship.kind === "tests" ? "tests" : "references";
      if (group === "tests" && !options.includeTests) continue;
      const reason = relationship.kind === "tests" ? "dependent test" : "dependent module";
      const existing = matches.find((match) => match.path === relationship.from && match.group === group);
      if (existing) {
        if (!existing.reasons.includes(reason)) existing.reasons.push(reason);
        existing.score = Math.max(existing.score, relationship.kind === "tests" ? 170 : 165);
        existing.confidence = "strong";
        continue;
      }
      if (matches.length >= candidateLimit) {
        candidateLimitReached = true;
        continue;
      }
      matches.push({
        path: relationship.from,
        line: 1,
        text: `${relationship.kind} ${relationship.to}`,
        group,
        score: relationship.kind === "tests" ? 170 : 165,
        reasons: [reason, `${relationship.kind} relationship`],
        confidence: "strong",
        source: relationship.source
      });
    }
  }

  if (candidateLimitReached) warnings.push(`Grouped search retained the first ${candidateLimit} candidates before ranking.`);

  assertSearchActive();
  for (const match of sortStructuredMatches(matches).slice(0, resultLimit)) groups[match.group].push(match);
  assertSearchActive();
  return {
    schemaVersion: 1,
    query,
    intent,
    groups,
    matches: Object.values(groups).flat(),
    coverage: {
      ...analysis.coverage,
      truncated: analysis.coverage.truncated || searchBudgetReached || candidateLimitReached || skippedFiles > 0,
      warnings
    },
    warnings,
    cache: analysis.cache
  };
}

export { invalidateWorkspaceAnalysis } from "./cache.js";
export { reviewWorkspaceChanges } from "./impact.js";
export { listAnalysisProviders, normalizeProviderPaths, registerAnalysisProvider } from "./providers.js";
export type * from "./types.js";

const MAX_ANALYSIS_INFLIGHT = 8;
const DEFAULT_ANALYSIS_DEADLINE_MS = 15_000;
const MAX_ANALYSIS_DEADLINE_MS = 60_000;
const analysisInflight = new Map<string, Promise<WorkspaceAnalysis>>();
let analysisBuildsStarted = 0;

export interface AnalysisExecutionOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

export function analysisRuntimeSnapshot(): {
  inflight: number;
  inflightLimit: number;
  buildsStarted: number;
  defaultDeadlineMs: number;
} {
  return {
    inflight: analysisInflight.size,
    inflightLimit: MAX_ANALYSIS_INFLIGHT,
    buildsStarted: analysisBuildsStarted,
    defaultDeadlineMs: DEFAULT_ANALYSIS_DEADLINE_MS
  };
}

function analysisScopeKey(workspace: Workspace, config: CodexProConfig, root: string): string {
  return `${workspace.id}:${root}:${JSON.stringify(config.analysisLimits)}`;
}

function boundedAnalysisDeadline(value?: number): number {
  if (!Number.isFinite(value)) return DEFAULT_ANALYSIS_DEADLINE_MS;
  return Math.max(1, Math.min(Math.floor(value as number), MAX_ANALYSIS_DEADLINE_MS));
}

async function awaitSharedAnalysis<T>(promise: Promise<T>, signal?: AbortSignal, timeoutMs?: number): Promise<T> {
  signal?.throwIfAborted();
  const waitMs = boundedAnalysisDeadline(timeoutMs);

  return new Promise<T>((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout>;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      callback();
    };
    const onAbort = () => finish(() => reject(signal?.reason ?? new Error("Repository analysis cancelled.")));
    timer = setTimeout(
      () => finish(() => reject(new Error(`Repository analysis waiter exceeded ${waitMs}ms deadline.`))),
      waitMs
    );
    timer.unref?.();
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) onAbort();
    promise.then(
      (value) => finish(() => resolve(value)),
      (error) => finish(() => reject(error))
    );
  });
}
