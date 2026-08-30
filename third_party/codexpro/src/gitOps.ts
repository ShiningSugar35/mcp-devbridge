import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import type { CodexProConfig } from "./config.js";
import type { Workspace } from "./guard.js";
import { PathGuard } from "./guard.js";
import { redactSensitiveText } from "./redact.js";

interface GitContext {
  cwd: string;
  pathspec?: string;
}

const MAX_GIT_DIAGNOSTIC_BYTES = 4 * 1024;
const MAX_GIT_DIAGNOSTIC_LINES = 8;
const MAX_GIT_DIFF_DURATION_MS = 30_000;

interface BoundedCapture {
  chunks: Buffer[];
  bytes: number;
  truncated: boolean;
}

function boundedUtf8(text: string, maxBytes: number): string {
  const source = Buffer.from(text, "utf8");
  if (source.byteLength <= maxBytes) return text;
  let end = maxBytes;
  while (end > 0 && (source[end] & 0xc0) === 0x80) end -= 1;
  return `${source.subarray(0, end).toString("utf8").trimEnd()}\n[git diagnostic truncated]`;
}

function compactGitFailure(output: string): string {
  const redacted = redactSensitiveText(output.trim());
  if (!redacted) return "git failed without diagnostic output";
  const lines = redacted.split(/\r?\n/);
  const usageIndex = lines.findIndex((line) => /^usage:\s+git\b/i.test(line.trim()));
  const diagnosticLines = (usageIndex >= 0 ? lines.slice(0, usageIndex) : lines).slice(
    0,
    MAX_GIT_DIAGNOSTIC_LINES
  );
  const selected = (diagnosticLines.length ? diagnosticLines : lines.slice(0, 1)).join("\n").trim();
  const truncated = usageIndex >= 0 || lines.length > diagnosticLines.length;
  return boundedUtf8(
    `${selected}${truncated ? "\n[git diagnostic truncated]" : ""}`,
    MAX_GIT_DIAGNOSTIC_BYTES
  );
}

function appendBoundedCapture(capture: BoundedCapture, chunk: Buffer, maxBytes: number): void {
  const remaining = Math.max(0, maxBytes - capture.bytes);
  if (remaining > 0) {
    const retained = chunk.subarray(0, remaining);
    capture.chunks.push(retained);
    capture.bytes += retained.byteLength;
  }
  if (chunk.byteLength > remaining) capture.truncated = true;
}

function captureText(capture: BoundedCapture): string {
  return Buffer.concat(capture.chunks, capture.bytes).toString("utf8").trim();
}

function runGitDiffBoundedAt(
  cwd: string,
  args: string[],
  maxOutputBytes: number,
  timeoutMs = MAX_GIT_DIFF_DURATION_MS
): Promise<string> {
  const outputLimit = Math.max(4_096, maxOutputBytes);
  const stdout: BoundedCapture = { chunks: [], bytes: 0, truncated: false };
  const stderr: BoundedCapture = { chunks: [], bytes: 0, truncated: false };

  return new Promise((resolve) => {
    let settled = false;
    let timedOut = false;
    const child = spawn("git", args, {
      cwd,
      env: { ...process.env, NO_COLOR: "1" },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    });

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill();
      } catch {
        // The close/error handler still settles the promise.
      }
    }, timeoutMs);
    timer.unref?.();

    const finish = (value: string): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    child.stdout?.on("data", (chunk: Buffer) => appendBoundedCapture(stdout, chunk, outputLimit));
    child.stderr?.on("data", (chunk: Buffer) =>
      appendBoundedCapture(stderr, chunk, MAX_GIT_DIAGNOSTIC_BYTES)
    );

    child.once("error", (error) => {
      const detail = [
        `git unavailable or failed: ${error.message}`,
        captureText(stderr),
        captureText(stdout)
      ].filter(Boolean).join("\n");
      finish(compactGitFailure(detail));
    });

    child.once("close", (code, signal) => {
      const stdoutText = captureText(stdout);
      const stderrText = captureText(stderr);
      if (timedOut) {
        if (stdoutText) {
          finish(
            `${redactSensitiveText(stdoutText)}\n[git diff truncated after ${timeoutMs}ms; narrow the path to inspect remaining changes]`
          );
          return;
        }
        finish(
          compactGitFailure(
            [`git diff exceeded ${timeoutMs}ms deadline`, stderrText, signal ? `signal: ${signal}` : ""]
              .filter(Boolean)
              .join("\n")
          )
        );
        return;
      }
      if (code !== 0) {
        finish(compactGitFailure(stderrText || stdoutText || `git exited with status ${code}`));
        return;
      }
      const output = redactSensitiveText(stdoutText || "(no output)");
      finish(
        stdout.truncated
          ? `${output}\n[git diff truncated at ${outputLimit} bytes; narrow the path to inspect remaining changes]`
          : output
      );
    });
  });
}

function runGitAt(cwd: string, args: string[], maxOutputBytes: number): string {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: maxOutputBytes,
    windowsHide: true,
    env: { ...process.env, NO_COLOR: "1" }
  });
  const stderr = result.stderr?.trim() || "";
  const stdout = result.stdout?.trim() || "";
  if (result.error) {
    const detail = [
      `git unavailable or failed: ${result.error.message}`,
      stderr,
      stdout
    ].filter(Boolean).join("\n");
    return compactGitFailure(detail);
  }
  if (result.status !== 0) {
    return compactGitFailure(stderr || stdout || `git exited with status ${result.status}`);
  }
  return redactSensitiveText(result.stdout.trim() || "(no output)");
}

function runGit(workspace: Workspace, args: string[], maxOutputBytes: number): string {
  return runGitAt(workspace.root, args, maxOutputBytes);
}

function gitContext(workspace: Workspace, guard: PathGuard, filePath: string): GitContext {
  const resolved = guard.resolve(workspace, filePath);
  let probe = resolved.absPath;
  try {
    if (!fs.statSync(probe).isDirectory()) probe = path.dirname(probe);
  } catch {
    probe = path.dirname(probe);
  }

  const result = spawnSync("git", ["-C", probe, "rev-parse", "--show-toplevel"], {
    windowsHide: true,
    cwd: workspace.root,
    encoding: "utf8",
    env: { ...process.env, NO_COLOR: "1" }
  });
  if (result.error || result.status !== 0 || !result.stdout?.trim()) {
    return { cwd: workspace.root, pathspec: resolved.relPath };
  }

  const repoRoot = path.resolve(result.stdout.trim());
  guard.resolve(workspace, repoRoot);
  const relative = path.relative(repoRoot, resolved.absPath);
  const pathspec = relative && relative !== "." ? relative.split(path.sep).join("/") : undefined;
  return { cwd: repoRoot, pathspec };
}

function isGitFailure(output: string): boolean {
  const trimmed = output.trim().toLowerCase();
  return (
    trimmed.startsWith("fatal:") ||
    trimmed.startsWith("error:") ||
    trimmed.startsWith("git unavailable or failed:") ||
    trimmed.startsWith("git exited with status") ||
    trimmed.startsWith("usage: git ") ||
    trimmed.includes("not a git repository")
  );
}

function outputLines(output: string): string[] {
  return output.trim() === "(no output)" ? [] : output.split("\n").map((line) => line.trim()).filter(Boolean);
}

function workspaceRelativePrefix(workspace: Workspace, cwd: string): string {
  const relative = path.relative(workspace.root, cwd);
  return relative && relative !== "." ? relative.split(path.sep).join("/") : "";
}

function prefixRepoPath(prefix: string, value: string): string {
  const normalized = value.split("\\").join("/");
  return prefix ? path.posix.join(prefix, normalized) : normalized;
}

function prefixNameStatus(output: string, prefix: string): string {
  if (!prefix || isGitFailure(output)) return output;
  const lines = outputLines(output).map((line) => {
    const parts = line.split("\t");
    if (parts.length < 2) return line;
    return [parts[0], ...parts.slice(1).map((value) => prefixRepoPath(prefix, value))].join("\t");
  });
  return lines.length ? lines.join("\n") : "(no output)";
}

export function gitStatus(config: CodexProConfig, workspace: Workspace, guard?: PathGuard, filePath?: string, staged = false): string {
  const args = staged ? ["diff", "--cached", "--name-status"] : ["status", "--short", "--branch"];
  let cwd = workspace.root;
  if (filePath?.trim()) {
    if (!guard) return "path-scoped git status requires a path guard";
    const context = gitContext(workspace, guard, filePath);
    cwd = context.cwd;
    if (context.pathspec) args.push("--", context.pathspec);
  }
  return runGitAt(cwd, args, config.maxOutputBytes);
}

export async function gitDiff(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  filePath?: string,
  staged = false
): Promise<string> {
  const args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"];
  if (staged) args.push("--staged");
  let cwd = workspace.root;
  if (filePath?.trim()) {
    const context = gitContext(workspace, guard, filePath);
    cwd = context.cwd;
    if (context.pathspec) args.push("--", context.pathspec);
  }
  return await runGitDiffBoundedAt(cwd, args, config.maxOutputBytes);
}

export function gitDiffShortStat(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  filePath?: string,
  staged = false
): string {
  const args = ["diff", "--shortstat", "--no-color", "--no-ext-diff", "--no-textconv"];
  if (staged) args.push("--staged");
  let cwd = workspace.root;
  if (filePath?.trim()) {
    const context = gitContext(workspace, guard, filePath);
    cwd = context.cwd;
    if (context.pathspec) args.push("--", context.pathspec);
  }
  // --shortstat is intentionally bounded to one summary line. Stats-only review
  // must never materialize a potentially multi-megabyte unified diff in memory.
  return runGitAt(cwd, args, Math.min(config.maxOutputBytes, 64 * 1024));
}

export function gitDiffStatus(config: CodexProConfig, guard: PathGuard, workspace: Workspace, filePath?: string, staged = false): string {
  const args = ["diff", "--name-status"];
  if (staged) args.push("--staged");
  const untrackedArgs = ["ls-files", "--others", "--exclude-standard"];
  let cwd = workspace.root;
  if (filePath?.trim()) {
    const context = gitContext(workspace, guard, filePath);
    cwd = context.cwd;
    if (context.pathspec) {
      args.push("--", context.pathspec);
      untrackedArgs.push("--", context.pathspec);
    }
  }
  const diffStatus = runGitAt(cwd, args, config.maxOutputBytes);
  if (isGitFailure(diffStatus)) return diffStatus;
  const prefix = workspaceRelativePrefix(workspace, cwd);
  const scopedDiffStatus = prefixNameStatus(diffStatus, prefix);
  if (staged) return scopedDiffStatus;
  const untracked = runGitAt(cwd, untrackedArgs, config.maxOutputBytes);
  if (isGitFailure(untracked)) return scopedDiffStatus;
  const lines = [
    ...outputLines(scopedDiffStatus),
    ...outputLines(untracked).map((line) => `?? ${prefixRepoPath(prefix, line)}`)
  ];
  return lines.length ? lines.join("\n") : "(no output)";
}

export function gitLog(config: CodexProConfig, workspace: Workspace, maxCount = 8): string {
  const count = Math.max(1, Math.min(Math.floor(maxCount), 30));
  return runGit(workspace, ["log", `--max-count=${count}`, "--oneline", "--decorate"], config.maxOutputBytes);
}

export function assertGitCleanEnoughForWrite(_workspace: Workspace): void {
  return;
}
