import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import {
  BASH_TASK_WAIT_MAX_MS,
  BashTaskManager,
  normalizeWindowsEnvironment,
  resolveBashShell
} from "../dist/bashOps.js";
import { loadConfig } from "../dist/config.js";
import { PathGuard, WorkspaceManager } from "../dist/guard.js";

process.env.CODEXPRO_BASH_MODE = "full";
process.env.CODEXPRO_ALLOW_NO_HTTP_TOKEN = "1";

const config = loadConfig(["--root", process.cwd(), "--bash", "full"]);
const guard = new PathGuard(config);
const workspaces = new WorkspaceManager(config);
const workspace = workspaces.defaultWorkspace();
assert.equal(BASH_TASK_WAIT_MAX_MS, 30_000, "wait_task polling cap must stay at 30 seconds");
const tasks = new BashTaskManager();

const normalizedWindowsEnv = normalizeWindowsEnvironment({
  PATH: "preferred-path",
  Path: "shadow-path",
  SystemRoot: "C:\\Windows",
  SYSTEMROOT: "C:\\ShadowWindows",
  MixedCaseOnly: "kept"
});
assert.deepEqual(
  Object.keys(normalizedWindowsEnv).filter((key) => key.toLowerCase() === "path"),
  ["PATH"],
  "Windows environment normalization must keep exactly one PATH key"
);
assert.equal(normalizedWindowsEnv.PATH, "preferred-path", "the canonical PATH value must win deterministically");
assert.deepEqual(
  Object.keys(normalizedWindowsEnv).filter((key) => key.toLowerCase() === "systemroot"),
  ["SystemRoot"],
  "Windows environment normalization must keep exactly one SystemRoot key"
);
assert.equal(normalizedWindowsEnv.SystemRoot, "C:\\Windows");
assert.equal(normalizedWindowsEnv.MixedCaseOnly, "kept");

const startedAt = Date.now();
const started = tasks.start(
  config,
  guard,
  workspace,
  'node -e "setTimeout(() => console.log(\'ASYNC_OK\'), 350)"'
);
assert.ok(Date.now() - startedAt < 2_000, "task start must return immediately");
assert.ok(["running", "completed"].includes(started.status));

const completed = await tasks.wait(workspace, started.taskId, 5_000);
assert.equal(completed.status, "completed");
assert.equal(completed.exitCode, 0);
assert.match(completed.stdout, /ASYNC_OK/);

if (process.platform === "win32") {
  const originalPath = process.env.PATH;
  try {
    process.env.PATH = "";
    const noPathTasks = new BashTaskManager();
    const noPathStarted = noPathTasks.start(
      config,
      guard,
      workspace,
      "Write-Output 'ABSOLUTE_POWERSHELL_OK'"
    );
    process.env.PATH = originalPath;
    const noPathCompleted = await noPathTasks.wait(workspace, noPathStarted.taskId, 5_000);
    assert.equal(noPathCompleted.status, "completed");
    assert.equal(noPathCompleted.exitCode, 0);
    assert.match(noPathCompleted.stdout, /ABSOLUTE_POWERSHELL_OK/);
  } finally {
    process.env.PATH = originalPath;
  }
}

if (process.platform === "win32") {
  const resolvedShell = resolveBashShell(config);
  assert.ok(path.isAbsolute(resolvedShell.executable), "the default Windows shell must resolve to an absolute path");
  assert.equal(fs.existsSync(resolvedShell.executable), true, "the resolved Windows shell must exist before spawn");

  const missingRoot = path.join(process.cwd(), `__codexpro_missing_windows_root_${process.pid}_${Date.now()}`);
  assert.equal(fs.existsSync(missingRoot), false);
  assert.throws(
    () =>
      resolveBashShell(
        { ...config, shell: "powershell" },
        { SystemRoot: missingRoot, WINDIR: missingRoot, PATH: "" }
      ),
    /shell executable not found.*Windows PowerShell/i,
    "a missing supported shell must fail before spawn with an actionable error"
  );

  const missingCwd = `__codexpro_missing_cwd_${process.pid}_${Date.now()}`;
  assert.throws(
    () =>
      new BashTaskManager().start(
        config,
        guard,
        workspace,
        "Write-Output 'SHOULD_NOT_START'",
        { cwd: missingCwd }
      ),
    /working directory does not exist/i,
    "a missing cwd must be distinguished from a missing shell executable"
  );
}

const cancellable = tasks.start(
  config,
  guard,
  workspace,
  'node -e "setTimeout(() => console.log(\'TOO_LATE\'), 30000)"'
);
assert.equal(cancellable.status, "running");
const cancelling = tasks.cancel(workspace, cancellable.taskId);
assert.ok(["cancelling", "cancelled"].includes(cancelling.status));
const cancelled = await tasks.wait(workspace, cancellable.taskId, 5_000);
assert.equal(cancelled.status, "cancelled");

const listed = tasks.list(workspace);
assert.ok(listed.some((task) => task.taskId === started.taskId && task.status === "completed"));
assert.ok(listed.some((task) => task.taskId === cancellable.taskId && task.status === "cancelled"));

const watchdogTasks = new BashTaskManager(50);
const watchdogStarted = watchdogTasks.start(
  config,
  guard,
  workspace,
  'node -e "setTimeout(() => console.log(\'WATCHDOG_DONE\'), 30000)"'
);
await new Promise((resolve) => setTimeout(resolve, 100));
const staleSnapshot = watchdogTasks.get(workspace, watchdogStarted.taskId);
assert.equal(staleSnapshot.orchestrationStale, true);
assert.equal(staleSnapshot.orchestrationStaleAfterMs, 50);
assert.match(staleSnapshot.resumeHint ?? "", /no execution timeout/i);
assert.match(staleSnapshot.resumeHint ?? "", /current assistant turn/i);
assert.equal(staleSnapshot.status, "running", "stale orchestration metadata must never terminate the task");
const resumedSnapshot = watchdogTasks.get(workspace, watchdogStarted.taskId);
assert.equal(
  resumedSnapshot.orchestrationStale,
  false,
  "reading a stale task should refresh the orchestration observation timestamp"
);
watchdogTasks.cancel(workspace, watchdogStarted.taskId);
const watchdogCancelled = await watchdogTasks.wait(workspace, watchdogStarted.taskId, 5_000);
assert.equal(watchdogCancelled.status, "cancelled");

const boundedTasks = new BashTaskManager(undefined, 2);
const boundedA = boundedTasks.start(
  config,
  guard,
  workspace,
  'node -e "setTimeout(() => console.log(\'BOUND_A\'), 30000)"'
);
const boundedB = boundedTasks.start(
  config,
  guard,
  workspace,
  'node -e "setTimeout(() => console.log(\'BOUND_B\'), 30000)"'
);
assert.throws(
  () =>
    boundedTasks.start(
      config,
      guard,
      workspace,
      'node -e "setTimeout(() => console.log(\'BOUND_C\'), 30000)"'
    ),
  /active|concurrent|limit|上限/i,
  "a third running task must be rejected instead of growing the active task map without a hard bound"
);
boundedTasks.cancel(workspace, boundedA.taskId);
boundedTasks.cancel(workspace, boundedB.taskId);
await boundedTasks.wait(workspace, boundedA.taskId, 5_000);
await boundedTasks.wait(workspace, boundedB.taskId, 5_000);

console.log("async-task-smoke: ok");
