import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

if (process.platform !== "win32") {
  console.log("windows-shell-runtime-smoke: skipped (non-Windows)");
  process.exit(0);
}

const targetDist = path.resolve(process.argv[2] ?? path.join(process.cwd(), "dist"));
const required = ["bashOps.js", "config.js", "guard.js"];
for (const name of required) {
  assert.equal(
    fs.existsSync(path.join(targetDist, name)),
    true,
    `target runtime is missing ${name}: ${targetDist}`
  );
}

const importFromDist = async (name) => import(pathToFileURL(path.join(targetDist, name)).href);
const [{ BashTaskManager }, { loadConfig }, { PathGuard, WorkspaceManager }] = await Promise.all([
  importFromDist("bashOps.js"),
  importFromDist("config.js"),
  importFromDist("guard.js")
]);

process.env.CODEXPRO_BASH_MODE = "full";
process.env.CODEXPRO_ALLOW_NO_HTTP_TOKEN = "1";

const config = loadConfig(["--root", process.cwd(), "--bash", "full"]);
const guard = new PathGuard(config);
const workspaces = new WorkspaceManager(config);
const workspace = workspaces.defaultWorkspace();
const tasks = new BashTaskManager();

const originalPath = process.env.PATH;
let started;
try {
  process.env.PATH = "";
  started = tasks.start(
    config,
    guard,
    workspace,
    "Write-Output 'WINDOWS_RUNTIME_SHELL_OK'"
  );
} finally {
  process.env.PATH = originalPath;
}

assert.ok(started?.taskId, "PATH-less runtime launch must return a task_id");
const completed = await tasks.wait(workspace, started.taskId, 5_000);
assert.equal(completed.status, "completed", completed.error ?? completed.stderr);
assert.equal(completed.exitCode, 0, completed.error ?? completed.stderr);
assert.match(completed.stdout, /WINDOWS_RUNTIME_SHELL_OK/);

console.log(`windows-shell-runtime-smoke: ok (${targetDist})`);
