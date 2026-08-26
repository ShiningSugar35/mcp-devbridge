import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { PathGuard, WorkspaceManager } from "../dist/guard.js";

const base = fs.mkdtempSync(path.join(os.tmpdir(), "codexpro-system-access-"));
const root = path.join(base, "root");
const outside = path.join(base, "outside");
fs.mkdirSync(root);
fs.mkdirSync(outside);
fs.writeFileSync(path.join(outside, "safe.txt"), "outside-ok\n", "utf8");
fs.writeFileSync(path.join(outside, ".env"), "SECRET=blocked\n", "utf8");

const blockedGlobs = [".env", ".env.*", "**/.env", "**/.env.*", "**/.ssh/**"];
const restrictedConfig = {
  defaultRoot: fs.realpathSync.native(root),
  allowedRoots: [fs.realpathSync.native(root)],
  systemAccess: false,
  blockedGlobs
};
const restrictedWorkspaces = new WorkspaceManager(restrictedConfig);
const restrictedWorkspace = restrictedWorkspaces.defaultWorkspace();
const restrictedGuard = new PathGuard(restrictedConfig);
assert.throws(() => restrictedWorkspaces.openWorkspace(outside), /outside allowed roots/);
assert.throws(
  () => restrictedGuard.resolve(restrictedWorkspace, path.join(outside, "safe.txt")),
  /escapes workspace root/
);

const systemConfig = { ...restrictedConfig, systemAccess: true };
const systemWorkspaces = new WorkspaceManager(systemConfig);
const systemWorkspace = systemWorkspaces.defaultWorkspace();
const openedOutside = systemWorkspaces.openWorkspace(outside);
assert.equal(openedOutside.root, fs.realpathSync.native(outside));
assert.equal(systemWorkspaces.openWorkspace(outside).id, openedOutside.id);

const systemGuard = new PathGuard(systemConfig);
const resolvedOutside = systemGuard.resolve(systemWorkspace, path.join(outside, "safe.txt"));
assert.equal(resolvedOutside.absPath, fs.realpathSync.native(path.join(outside, "safe.txt")));
const writableOutside = systemGuard.resolve(systemWorkspace, path.join(outside, "created.txt"), { forWrite: true });
assert.equal(path.dirname(writableOutside.absPath), fs.realpathSync.native(outside));
assert.throws(
  () => systemGuard.resolve(systemWorkspace, path.join(outside, ".env")),
  /blocked by safety rules/
);

fs.rmSync(base, { recursive: true, force: true });
console.log("system-access smoke: ok");
