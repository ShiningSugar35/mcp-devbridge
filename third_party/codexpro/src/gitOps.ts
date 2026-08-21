import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import type { CodexProConfig } from "./config.js";
import type { Workspace } from "./guard.js";
import { PathGuard } from "./guard.js";
import { redactSensitiveText } from "./redact.js";

interface GitContext {
  cwd: string;
  pathspec?: string;
}

function runGitAt(cwd: string, args: string[], maxOutputBytes: number): string {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: maxOutputBytes,
    env: { ...process.env, NO_COLOR: "1" }
  });
  if (result.error) {
    return `git unavailable or failed: ${result.error.message}`;
  }
  if (result.status !== 0) {
    const stderr = result.stderr?.trim() || "";
    const stdout = result.stdout?.trim() || "";
    return stderr || stdout || `git exited with status ${result.status}`;
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

export function gitDiff(config: CodexProConfig, guard: PathGuard, workspace: Workspace, filePath?: string, staged = false): string {
  const args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"];
  if (staged) args.push("--staged");
  let cwd = workspace.root;
  if (filePath?.trim()) {
    const context = gitContext(workspace, guard, filePath);
    cwd = context.cwd;
    if (context.pathspec) args.push("--", context.pathspec);
  }
  return runGitAt(cwd, args, config.maxOutputBytes);
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
