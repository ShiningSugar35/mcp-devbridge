import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { CodexProConfig } from "./config.js";
import type { Workspace } from "./guard.js";
import { CodexProError, PathGuard } from "./guard.js";
import { redactSensitiveText } from "./redact.js";

export interface BashResult {
  command: string;
  cwd: string;
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  durationMs: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  bashSessionId?: string;
}

export const BASH_TASK_WAIT_MAX_MS = 30_000;
export const BASH_ORCHESTRATION_STALE_MS = 600_000;

export type BashTaskStatus = "running" | "cancelling" | "completed" | "failed" | "cancelled";

export interface BashTaskSnapshot extends BashResult {
  taskId: string;
  workspaceId: string;
  status: BashTaskStatus;
  pid: number | null;
  startedAt: string;
  lastObservedAt: string;
  orchestrationStale: boolean;
  orchestrationStaleAfterMs: number;
  finishedAt?: string;
  error?: string;
  resumeHint?: string;
}

interface BashTaskInternal {
  taskId: string;
  workspaceId: string;
  command: string;
  cwd: string;
  status: BashTaskStatus;
  child: ChildProcess;
  startedAtMs: number;
  lastObservedAtMs: number;
  finishedAtMs?: number;
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
  truncated: boolean;
  bashSessionId?: string;
  error?: string;
  maxOutputBytes: number;
}

const SAFE_ALLOWED_PREFIXES = [
  "pwd",
  "ls",
  "find",
  "git status",
  "git diff",
  "git log",
  "git show",
  "git branch",
  "git rev-parse",
  "git ls-files",
  "npm test",
  "npm run test",
  "npm run typecheck",
  "npm run lint",
  "npm run build",
  "npm run check",
  "pnpm test",
  "pnpm run test",
  "pnpm run typecheck",
  "pnpm run lint",
  "pnpm run build",
  "pnpm run check",
  "yarn test",
  "yarn run test",
  "yarn run typecheck",
  "yarn run lint",
  "yarn run build",
  "yarn run check",
  "bun test",
  "bun run test",
  "bun run typecheck",
  "bun run lint",
  "bun run build",
  "pytest",
  "python -m pytest",
  "python3 -m pytest",
  "uv run pytest",
  "go test",
  "cargo test",
  "cargo check",
  "cargo clippy",
  "tsc",
  "npx tsc",
  "eslint",
  "npx eslint",
  "biome check",
  "npx biome check"
];

const SAFE_BLOCKED_PATTERNS = [
  /(^|\s)rm\s+/,
  /(^|\s)mv\s+/,
  /(^|\s)cp\s+/,
  /(^|\s)dd\s+/,
  /(^|\s)sudo\s+/,
  /(^|\s)chmod\s+/,
  /(^|\s)chown\s+/,
  /(^|\s)kill\s+/,
  /(^|\s)pkill\s+/,
  /(^|\s)curl\s+/,
  /(^|\s)wget\s+/,
  /(^|\s)ssh\s+/,
  /(^|\s)scp\s+/,
  /(^|\s)rsync\s+/,
  /(^|\s)docker\s+/,
  /(^|\s)podman\s+/,
  /(^|\s)git\s+push\b/,
  /(^|\s)git\s+reset\b/,
  /(^|\s)git\s+clean\b/,
  /(^|\s)git\s+checkout\b/,
  /(^|\s)git\s+switch\b/,
  /(^|\s)git\s+restore\b/,
  /(^|\s)(npm|pnpm|yarn)\s+publish\b/,
  /(^|\s)--no-index\b/,
  /(^|\s)--fix\b/,
  /(^|\s)(\/|~(?:\/|\s|$))/,
  /(^|\s)\.\.(?:\/|\s|$)/,
  /\$/,
  /(^|[\s:])(?:\.env(?:[./\s:]|$)|\.git(?:[\/\s:]|$)|node_modules(?:[\/\s:]|$)|\.ssh(?:[\/\s:]|$)|id_rsa(?:[.\s:]|$)|id_ed25519(?:[.\s:]|$)|[^\s:]*\.(?:pem|key)(?:[\s:]|$))/,
  /(^|\s)['"]?-exec(?:['"]|\s|$)/,
  /(^|\s)['"]?-execdir(?:['"]|\s|$)/,
  /(^|\s)['"]?-delete(?:['"]|\s|$)/,
  /(^|\s)['"]?-ok(?:['"]|\s|$)/,
  /(^|\s)['"]?-okdir(?:['"]|\s|$)/,
  /(^|\s)['"]?-fprint0?(?:['"]|\s|$)/,
  /(^|\s)['"]?-fprintf(?:['"]|\s|$)/,
  /(^|\s)['"]?-fls(?:['"]|\s|$)/,
  /(^|\s)['"]?--output(?:=|['"]|\s|$)/,
  /(^|\s)(sed|perl)\s+.*(^|\s)-i(\s|$)/,
  /(^|\s)(cat|grep|rg|head|tail|wc)\s+/,
  /[;&|<>`]/,
  /[\r\n]/
];

// "developer" 档：只允许开发工具作为第一条命令（pytest / pyright / ruff / git
// 完整子命令 / npm / pnpm / yarn / bun / uv / python ...）。仍拦截一切系统破坏
// 或危险命令。与 safe 档的区别：allowlist 从“固定子命令前缀”放宽为“命令首词”，
// 允许 git checkout / git push / npm install 等日常开发命令。
const DEVELOPER_ALLOWED_BASES = new Set<string>([
  "pwd", "ls", "dir", "echo", "cd", "cat", "type", "clear",
  "python", "python3", "py", "uv", "uvx", "pip", "pip3",
  "pytest", "pyright", "ruff", "mypy",
  "node", "node.exe", "npm", "npx", "pnpm", "yarn", "bun",
  "tsc", "npx", "eslint", "biome",
  "git", "git.exe", "bash",
  "pwsh", "powershell",
  "dotnet", "cargo", "go", "java", "mvn", "gradle",
]);

const DEVELOPER_BLOCKED_PATTERNS = [
  /(^|\s)(rm|del|erase|rd|rmdir)\s+/,
  /(^|\s)(mv|move|cp|ren)\s+/i,
  /(^|\s)(dd|format|diskpart|shutdown|reboot|bcdedit|diskperf)\b/i,
  /(^|\s)(sudo|reg\s+delete|chkdsk)\b/i,
  /(^|\s)(kill|taskkill|pkill)\s+/i,
  /(^|\s)(\.\.(\/|\\))/,
  /\$/,
  /[;&|<>`]/,
  /[\r\n]/
];

function firstBase(command: string): string {
  const word = command.trim().split(/\s+/)[0] ?? "";
  const cleaned = word.replace(/^['"]|['"]$/g, "");
  return cleaned.split(/[\\/]/).pop()!.toLowerCase();
}

function compact(command: string): string {
  return command.trim().replace(/\s+/g, " ");
}

function startsWithAllowedPrefix(command: string): boolean {
  const normalized = compact(command);
  return isAllowedPackageScript(normalized) || SAFE_ALLOWED_PREFIXES.some((prefix) => normalized === prefix || normalized.startsWith(`${prefix} `));
}

function isAllowedPackageScript(command: string): boolean {
  const packageScriptPattern =
    /^(?:npm|pnpm|yarn|bun)\s+run\s+(?:test|typecheck|lint|build|check)(?::[A-Za-z0-9._-]+)*(?:\s+--\s+[A-Za-z0-9._:= -]+)?$/;
  return packageScriptPattern.test(command);
}

function assertSafeCommand(config: CodexProConfig, command: string): void {
  if (config.bashMode === "off") {
    throw new CodexProError("bash tool is disabled. Start with CODEXPRO_BASH_MODE=safe, developer, or full to enable it.");
  }
  if (config.bashMode === "full") return;

  const raw = command.trim();
  const normalized = compact(command);

  if (config.bashMode === "developer") {
    for (const pattern of DEVELOPER_BLOCKED_PATTERNS) {
      if (pattern.test(raw) || pattern.test(normalized)) {
        throw new CodexProError(
          `Command is blocked in CODEXPRO_BASH_MODE=developer: ${normalized}\n` +
            "Developer profile blocks system-destructive commands (format / diskpart / rm / del / reg delete / shutdown ...)."
        );
      }
    }
    const base = firstBase(normalized);
    if (base && DEVELOPER_ALLOWED_BASES.has(base)) return;
    throw new CodexProError(
      `Command is not in the developer allowlist: ${normalized}\n` +
        "developer 档只允许开发工具（pytest / pyright / ruff / git / npm / pnpm / yarn / bun / uv / python / node / tsc / eslint / cargo / go ...）。" +
        "切换档位或使用文本/文件专用工具完成此操作。"
    );
  }

  for (const pattern of SAFE_BLOCKED_PATTERNS) {
    if (pattern.test(raw) || pattern.test(normalized)) {
      throw new CodexProError(
        `Command is blocked in CODEXPRO_BASH_MODE=safe: ${normalized}\n` +
          "Use separate read/search/git tools, or restart with CODEXPRO_BASH_MODE=developer/full only for trusted repos."
      );
    }
  }
  if (!startsWithAllowedPrefix(normalized)) {
    throw new CodexProError(
      `Command is not in the safe bash allowlist: ${normalized}\n` +
        "Allowed examples: ls, find, git status, git diff, npm test, npm run typecheck, npm run build:clients, pytest, go test, cargo test. Use read/search tools for file contents. " +
        "Use CODEXPRO_BASH_MODE=developer for read/test/lint commands or CODEXPRO_BASH_MODE=full for trusted local automation."
    );
  }
}

function assertBashSession(config: CodexProConfig, sessionId?: string): string | undefined {
  const requested = sessionId?.trim();
  if (!config.bashSessionId) {
    if (config.requireBashSession) {
      throw new CodexProError("bash session guard is enabled but no server bash session id is configured.");
    }
    return undefined;
  }
  if (!requested) {
    if (config.requireBashSession) {
      throw new CodexProError(`bash session id is required. Retry with session_id="${config.bashSessionId}".`);
    }
    return config.bashSessionId;
  }
  if (requested !== config.bashSessionId) {
    throw new CodexProError(`bash session id mismatch. This CodexPro server accepts session_id="${config.bashSessionId}".`);
  }
  return config.bashSessionId;
}

function makeEnv(config: CodexProConfig): NodeJS.ProcessEnv {
  if (config.inheritEnv || process.platform === "win32") {
    return { ...process.env, NO_COLOR: "1", CI: process.env.CI ?? "1" };
  }
  return {
    PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
    HOME: process.env.HOME ?? "",
    USER: process.env.USER ?? "",
    SHELL: process.env.SHELL ?? "/bin/bash",
    TMPDIR: process.env.TMPDIR ?? "/tmp",
    TERM: "dumb",
    NO_COLOR: "1",
    CI: "1"
  };
}

function shellInfo(config: CodexProConfig): { executable: string; shellArgs: string[] } {
  const customShell = config.shell?.trim().toLowerCase();
  if (customShell) {
    if (customShell === "powershell" || customShell === "pwsh" || customShell === "windows_powershell") {
      return { executable: "powershell.exe", shellArgs: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"] };
    }
    if (customShell === "cmd") {
      return { executable: "cmd.exe", shellArgs: ["/c"] };
    }
    if (customShell === "bash" || customShell === "git-bash") {
      return { executable: "bash", shellArgs: ["-lc"] };
    }
    return { executable: customShell, shellArgs: ["-c"] };
  }
  if (process.platform === "win32") {
    return { executable: "powershell.exe", shellArgs: ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"] };
  }
  return { executable: fs.existsSync("/bin/bash") ? "/bin/bash" : "bash", shellArgs: ["-lc"] };
}

function trimOutput(value: string, maxBytes: number): { value: string; truncated: boolean } {
  const buffer = Buffer.from(value, "utf8");
  if (buffer.byteLength <= maxBytes) return { value, truncated: false };
  const sliced = buffer.subarray(0, maxBytes).toString("utf8");
  return { value: `${sliced}\n...[output truncated to ${maxBytes} bytes]`, truncated: true };
}

function terminateProcessTree(child: ChildProcess, signal: NodeJS.Signals): void {
  if (!child.pid) return;
  if (process.platform === "win32") {
    // Windows does not provide Unix-style cooperative signals to process trees.
    // Force the full tree while the parent PID still identifies its descendants;
    // otherwise the shell can exit first and orphan an output-heavy grandchild.
    const args = ["/pid", String(child.pid), "/t", "/f"];
    const result = spawnSync("taskkill", args, { stdio: "ignore", windowsHide: true });
    if (result.status !== 0) child.kill(signal);
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") child.kill(signal);
  }
}

function appendRollingOutput(current: string, chunk: unknown, maxBytes: number): { value: string; truncated: boolean } {
  const incoming = Buffer.from(String(chunk), "utf8");
  const merged = Buffer.concat([Buffer.from(current, "utf8"), incoming]);
  if (merged.byteLength <= maxBytes) return { value: merged.toString("utf8"), truncated: false };
  const marker = Buffer.from("...[earlier output omitted]...\n", "utf8");
  const payloadBytes = Math.max(1, maxBytes - marker.byteLength);
  const tail = merged.subarray(Math.max(0, merged.byteLength - payloadBytes));
  return { value: Buffer.concat([marker, tail]).toString("utf8"), truncated: true };
}

function taskTerminal(status: BashTaskStatus): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

export class BashTaskManager {
  private readonly tasks = new Map<string, BashTaskInternal>();
  private readonly retentionMs = 24 * 60 * 60 * 1_000;
  private readonly maxTasks = 100;

  constructor(private readonly orchestrationStaleMs = BASH_ORCHESTRATION_STALE_MS) {}

  private prune(): void {
    const now = Date.now();
    for (const [taskId, task] of this.tasks) {
      if (taskTerminal(task.status) && task.finishedAtMs && now - task.finishedAtMs > this.retentionMs) {
        this.tasks.delete(taskId);
      }
    }
    if (this.tasks.size <= this.maxTasks) return;
    const removable = [...this.tasks.values()]
      .filter((task) => taskTerminal(task.status))
      .sort((left, right) => (left.finishedAtMs ?? left.startedAtMs) - (right.finishedAtMs ?? right.startedAtMs));
    while (this.tasks.size > this.maxTasks && removable.length) {
      const task = removable.shift();
      if (task) this.tasks.delete(task.taskId);
    }
  }

  private find(workspace: Workspace, taskId: string): BashTaskInternal {
    this.prune();
    const task = this.tasks.get(taskId);
    if (!task || task.workspaceId !== workspace.id) {
      throw new CodexProError(`Async task not found in this workspace: ${taskId}`);
    }
    return task;
  }

  private snapshot(task: BashTaskInternal, touchObservation = false): BashTaskSnapshot {
    const now = Date.now();
    const finished = task.finishedAtMs;
    const lastObservedAtMs = task.lastObservedAtMs;
    const orchestrationStale = now - lastObservedAtMs >= this.orchestrationStaleMs;
    if (touchObservation) task.lastObservedAtMs = now;
    return {
      taskId: task.taskId,
      workspaceId: task.workspaceId,
      command: redactSensitiveText(task.command),
      cwd: task.cwd,
      status: task.status,
      pid: task.child.pid ?? null,
      startedAt: new Date(task.startedAtMs).toISOString(),
      lastObservedAt: new Date(lastObservedAtMs).toISOString(),
      orchestrationStale,
      orchestrationStaleAfterMs: this.orchestrationStaleMs,
      ...(finished ? { finishedAt: new Date(finished).toISOString() } : {}),
      durationMs: (finished ?? now) - task.startedAtMs,
      exitCode: task.exitCode,
      signal: task.signal,
      stdout: redactSensitiveText(task.stdout),
      stderr: redactSensitiveText(task.stderr),
      truncated: task.truncated,
      ...(task.bashSessionId ? { bashSessionId: task.bashSessionId } : {}),
      ...(task.error ? { error: redactSensitiveText(task.error) } : {}),
      ...(orchestrationStale
        ? {
            resumeHint: taskTerminal(task.status)
              ? "The orchestration was idle for at least 600 seconds. Read this terminal result and resume the workflow now; do not keep waiting."
              : "The orchestration was idle for at least 600 seconds. The task is still running; resume from this snapshot instead of entering an unbounded wait."
          }
        : {})
    };
  }

  start(
    config: CodexProConfig,
    guard: PathGuard,
    workspace: Workspace,
    command: string,
    options: { cwd?: string; sessionId?: string } = {}
  ): BashTaskSnapshot {
    if (!command?.trim()) throw new CodexProError("command is required.");
    const bashSessionId = assertBashSession(config, options.sessionId);
    assertSafeCommand(config, command);
    const cwdResolved = guard.resolve(workspace, options.cwd ?? ".");
    const cwd = cwdResolved.absPath;
    const { executable, shellArgs } = shellInfo(config);
    const child = spawn(executable, [...shellArgs, command], {
      cwd,
      env: makeEnv(config),
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
      windowsHide: true
    });
    const task: BashTaskInternal = {
      taskId: randomUUID(),
      workspaceId: workspace.id,
      command,
      cwd: path.relative(workspace.root, cwd) || ".",
      status: "running",
      child,
      startedAtMs: Date.now(),
      lastObservedAtMs: Date.now(),
      exitCode: null,
      signal: null,
      stdout: "",
      stderr: "",
      truncated: false,
      ...(bashSessionId ? { bashSessionId } : {}),
      maxOutputBytes: Math.max(16_384, config.maxOutputBytes)
    };
    this.tasks.set(task.taskId, task);
    this.prune();

    child.stdout?.on("data", (chunk) => {
      const next = appendRollingOutput(task.stdout, chunk, task.maxOutputBytes);
      task.stdout = next.value;
      task.truncated = task.truncated || next.truncated;
    });
    child.stderr?.on("data", (chunk) => {
      const next = appendRollingOutput(task.stderr, chunk, task.maxOutputBytes);
      task.stderr = next.value;
      task.truncated = task.truncated || next.truncated;
    });
    child.on("error", (error) => {
      if (taskTerminal(task.status)) return;
      task.status = "failed";
      task.error = `${error.name}: ${error.message}`;
      const next = appendRollingOutput(task.stderr, `\n[codexpro] ${task.error}\n`, task.maxOutputBytes);
      task.stderr = next.value;
      task.truncated = task.truncated || next.truncated;
      task.finishedAtMs = Date.now();
    });
    child.on("close", (exitCode, signal) => {
      task.exitCode = exitCode;
      task.signal = signal;
      task.finishedAtMs = Date.now();
      if (task.status === "cancelling") {
        task.status = "cancelled";
      } else if (task.status !== "failed") {
        task.status = exitCode === 0 ? "completed" : "failed";
      }
    });
    return this.snapshot(task);
  }

  get(workspace: Workspace, taskId: string): BashTaskSnapshot {
    return this.snapshot(this.find(workspace, taskId), true);
  }

  list(workspace: Workspace): BashTaskSnapshot[] {
    this.prune();
    return [...this.tasks.values()]
      .filter((task) => task.workspaceId === workspace.id)
      .sort((left, right) => right.startedAtMs - left.startedAtMs)
      .map((task) => this.snapshot(task, true));
  }

  async wait(workspace: Workspace, taskId: string, waitMs = 15_000): Promise<BashTaskSnapshot> {
    const deadline = Date.now() + Math.max(0, Math.min(waitMs, BASH_TASK_WAIT_MAX_MS));
    const task = this.find(workspace, taskId);
    while (true) {
      if (taskTerminal(task.status) || Date.now() >= deadline) return this.snapshot(task, true);
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }

  cancel(workspace: Workspace, taskId: string): BashTaskSnapshot {
    const task = this.find(workspace, taskId);
    if (!taskTerminal(task.status) && task.status !== "cancelling") {
      task.status = "cancelling";
      terminateProcessTree(task.child, "SIGTERM");
    }
    return this.snapshot(task, true);
  }
}

export async function runBash(
  config: CodexProConfig,
  guard: PathGuard,
  workspace: Workspace,
  command: string,
  options: { cwd?: string; timeoutMs?: number; sessionId?: string } = {}
): Promise<BashResult> {
  if (!command?.trim()) throw new CodexProError("command is required.");
  const bashSessionId = assertBashSession(config, options.sessionId);
  assertSafeCommand(config, command);
  const cwdResolved = guard.resolve(workspace, options.cwd ?? ".");
  const cwd = cwdResolved.absPath;
  const timeoutMs = Math.max(1_000, options.timeoutMs ?? 30_000);
  const start = Date.now();

  return new Promise((resolve, reject) => {
    const { executable, shellArgs } = shellInfo(config);
    const child = spawn(executable, [...shellArgs, command], {
      cwd,
      env: makeEnv(config),
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
      windowsHide: true
    });

    let stdout = "";
    let stderr = "";
    let killedByTimeout = false;
    let closed = false;
    let terminationStarted = false;
    let killTimer: NodeJS.Timeout | undefined;
    let observedOutputBytes = 0;
    const retainedOutputBytes = config.maxOutputBytes + 1;

    const terminate = (signal: NodeJS.Signals) => {
      if (closed) return;
      terminationStarted = true;
      terminateProcessTree(child, signal);
    };
    const terminateWithEscalation = () => {
      if (terminationStarted || closed) return;
      terminate("SIGTERM");
      killTimer = setTimeout(() => terminate("SIGKILL"), 1_500);
      killTimer.unref();
    };
    const appendBounded = (current: string, chunk: unknown) => {
      const bytes = Buffer.from(String(chunk), "utf8");
      observedOutputBytes += bytes.byteLength;
      const remaining = retainedOutputBytes - Buffer.byteLength(stdout, "utf8") - Buffer.byteLength(stderr, "utf8");
      if (remaining <= 0) return current;
      return current + bytes.subarray(0, remaining).toString("utf8");
    };

    const timer = setTimeout(() => {
      killedByTimeout = true;
      terminateWithEscalation();
    }, timeoutMs);
    timer.unref();

    child.stdout.on("data", (chunk) => {
      stdout = appendBounded(stdout, chunk);
      if (observedOutputBytes > config.maxOutputBytes) terminateWithEscalation();
    });
    child.stderr.on("data", (chunk) => {
      stderr = appendBounded(stderr, chunk);
      if (observedOutputBytes > config.maxOutputBytes) terminateWithEscalation();
    });
    child.on("error", reject);
    child.on("close", (exitCode, signal) => {
      closed = true;
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      if (killedByTimeout) {
        stderr += `\n[codexpro] Command timed out after ${timeoutMs} ms.`;
      }
      const out = trimOutput(redactSensitiveText(stdout), config.maxOutputBytes);
      const err = trimOutput(redactSensitiveText(stderr), config.maxOutputBytes);
      resolve({
        command,
        cwd: path.relative(workspace.root, cwd) || ".",
        exitCode,
        signal,
        durationMs: Date.now() - start,
        stdout: out.value,
        stderr: err.value,
        truncated: out.truncated || err.truncated,
        ...(bashSessionId ? { bashSessionId } : {})
      });
    });
  });
}
