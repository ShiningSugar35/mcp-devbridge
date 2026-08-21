import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { repoTree } from "../dist/fsOps.js";
import { PathGuard } from "../dist/guard.js";

const config = { blockedGlobs: [] };
let root;
let cleanup = () => {};
let expectWarning = false;

if (process.platform === "win32") {
  root = `${process.env.SystemDrive || "C:"}\\`;
} else {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "codexpro-root-scan-"));
  const readable = path.join(root, "readable");
  const blocked = path.join(root, "blocked");
  fs.mkdirSync(readable);
  fs.mkdirSync(blocked);
  fs.writeFileSync(path.join(readable, "ok.txt"), "ok\n", "utf8");
  fs.chmodSync(blocked, 0o000);
  expectWarning = typeof process.getuid === "function" && process.getuid() !== 0;
  cleanup = () => {
    try { fs.chmodSync(blocked, 0o700); } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  };
}

try {
  const workspace = { id: "root-scan", root, openedAt: new Date().toISOString() };
  const result = await repoTree(config, new PathGuard(config), workspace, {
    path: ".",
    maxDepth: 2,
    includeHidden: true,
    maxEntries: 5000
  });
  if (!Array.isArray(result.warnings)) throw new Error("tree result did not expose warnings");
  if (expectWarning && result.warnings.length === 0) {
    throw new Error("unreadable child was not reported as a warning");
  }
  console.log(`root scan smoke ok: ${result.entries} entries, ${result.warnings.length} warnings`);
} finally {
  cleanup();
}
