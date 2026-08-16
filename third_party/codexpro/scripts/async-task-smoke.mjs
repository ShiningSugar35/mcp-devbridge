import assert from "node:assert/strict";
import process from "node:process";
import { BashTaskManager } from "../dist/bashOps.js";
import { loadConfig } from "../dist/config.js";
import { PathGuard, WorkspaceManager } from "../dist/guard.js";

process.env.CODEXPRO_BASH_MODE = "full";
process.env.CODEXPRO_ALLOW_NO_HTTP_TOKEN = "1";

const config = loadConfig(["--root", process.cwd(), "--bash", "full"]);
const guard = new PathGuard(config);
const workspaces = new WorkspaceManager(config);
const workspace = workspaces.defaultWorkspace();
const tasks = new BashTaskManager();


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

console.log("async-task-smoke: ok");
