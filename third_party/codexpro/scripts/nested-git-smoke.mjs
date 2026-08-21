import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { gitDiffStatus, gitStatus } from "../dist/gitOps.js";
import { PathGuard } from "../dist/guard.js";

const root = fs.mkdtempSync(path.join(os.tmpdir(), "codexpro-nested-git-"));
const repo = path.join(root, "nested", "repo");
fs.mkdirSync(repo, { recursive: true });
const init = spawnSync("git", ["init", "-q", repo], { encoding: "utf8" });
if (init.status !== 0) throw new Error(init.stderr || "git init failed");
fs.writeFileSync(path.join(repo, "untracked.txt"), "ok\n", "utf8");

const config = { maxOutputBytes: 1_000_000, blockedGlobs: [] };
const workspace = { id: "root", root: fs.realpathSync.native(root), openedAt: new Date().toISOString() };
const guard = new PathGuard(config);
const output = gitStatus(config, workspace, guard, repo, false);
if (/not a git repository/i.test(output) || !output.includes("untracked.txt")) {
  throw new Error(`nested git routing failed: ${output}`);
}
const diffStatus = gitDiffStatus(config, guard, workspace, repo, false);
const expectedWorkspacePath = ["nested", "repo", "untracked.txt"].join("/");
if (!diffStatus.includes(`?? ${expectedWorkspacePath}`)) {
  throw new Error(`nested git diff-status path was not workspace-relative: ${diffStatus}`);
}

fs.rmSync(root, { recursive: true, force: true });
console.log("nested git smoke ok");
