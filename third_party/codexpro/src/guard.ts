import fs from "node:fs";
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { minimatch } from "minimatch";
import type { CodexProConfig } from "./config.js";
import { expandHome } from "./config.js";

const workspaceClientAffinity = new AsyncLocalStorage<string>();
const MAX_CLIENT_SELECTIONS = 2048;
const MAX_OPEN_WORKSPACES = 4096;

export function withWorkspaceClientAffinity<T>(affinity: string, fn: () => T): T {
  return workspaceClientAffinity.run(affinity.trim() || "default", fn);
}

function currentWorkspaceClientAffinity(): string {
  return workspaceClientAffinity.getStore() ?? "default";
}

export interface Workspace {
  id: string;
  root: string;
  openedAt: string;
}

export class CodexProError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CodexProError";
  }
}

export function isSubpath(child: string, parent: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export function normalizeRelPath(relPath: string): string {
  const normalized = relPath.split(path.sep).join("/");
  if (normalized === "") return ".";
  return normalized;
}

export function displayPath(absPath: string, root: string): string {
  const rel = path.relative(root, absPath) || ".";
  return normalizeRelPath(rel);
}

function workspaceIdForRoot(realRoot: string): string {
  return `ws_${createHash("sha256").update(realRoot).digest("hex").slice(0, 24)}`;
}

function maybeRealpath(existingPath: string): string | undefined {
  try {
    return fs.realpathSync.native(existingPath);
  } catch {
    return undefined;
  }
}

function closestExistingParent(absPath: string): string {
  let current = path.resolve(absPath);
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return current;
}

export class WorkspaceManager {
  private readonly workspaces = new Map<string, Workspace>();
  private readonly workspaceIdsByRoot = new Map<string, string>();
  private readonly selectedWorkspaceIds = new Map<string, string>();

  constructor(private readonly config: CodexProConfig) {}

  private rememberSelection(workspaceId: string): void {
    const key = currentWorkspaceClientAffinity();
    if (this.selectedWorkspaceIds.has(key)) this.selectedWorkspaceIds.delete(key);
    this.selectedWorkspaceIds.set(key, workspaceId);
    while (this.selectedWorkspaceIds.size > MAX_CLIENT_SELECTIONS) {
      const oldest = this.selectedWorkspaceIds.keys().next().value;
      if (typeof oldest !== "string") break;
      this.selectedWorkspaceIds.delete(oldest);
    }
  }

  defaultWorkspace(): Workspace {
    const existingId = this.workspaceIdsByRoot.get(this.config.defaultRoot);
    const existing = existingId ? this.workspaces.get(existingId) : undefined;
    return existing ?? this.openWorkspace(this.config.defaultRoot, { select: false });
  }

  selectDefaultWorkspace(): Workspace {
    const workspace = this.defaultWorkspace();
    this.rememberSelection(workspace.id);
    return workspace;
  }

  openWorkspace(rootInput?: string, options: { select?: boolean } = {}): Workspace {
    const requested = rootInput?.trim() ? expandHome(rootInput.trim()) : this.config.defaultRoot;
    const resolved = path.resolve(requested);
    if (!fs.existsSync(resolved)) {
      throw new CodexProError(`Workspace root does not exist: ${resolved}`);
    }
    const stat = fs.statSync(resolved);
    if (!stat.isDirectory()) {
      throw new CodexProError(`Workspace root is not a directory: ${resolved}`);
    }
    const realRoot = fs.realpathSync.native(resolved);
    const allowed = this.config.systemAccess || this.config.allowedRoots.some((allowedRoot) => isSubpath(realRoot, allowedRoot));
    if (!allowed) {
      throw new CodexProError(
        `Workspace root is outside allowed roots: ${realRoot}\nAllowed roots:\n${this.config.allowedRoots.map((r) => `- ${r}`).join("\n")}`
      );
    }

    const existingId = this.workspaceIdsByRoot.get(realRoot);
    const existing = existingId ? this.workspaces.get(existingId) : undefined;
    if (existing) {
      if (options.select !== false) this.rememberSelection(existing.id);
      return existing;
    }

    if (this.workspaces.size >= MAX_OPEN_WORKSPACES) {
      throw new CodexProError(
        `Workspace registry limit reached (${MAX_OPEN_WORKSPACES}). Restart CodexPro to clear inactive handles before opening another workspace.`
      );
    }
    const id = workspaceIdForRoot(realRoot);
    const workspace = { id, root: realRoot, openedAt: new Date().toISOString() };
    this.workspaces.set(id, workspace);
    this.workspaceIdsByRoot.set(realRoot, id);
    if (options.select !== false) this.rememberSelection(id);
    return workspace;
  }

  getWorkspace(id?: string): Workspace {
    if (!id) {
      const selectedWorkspaceId = this.selectedWorkspaceIds.get(currentWorkspaceClientAffinity());
      if (selectedWorkspaceId) {
        const selected = this.workspaces.get(selectedWorkspaceId);
        if (selected) return selected;
      }
      return this.selectDefaultWorkspace();
    }
    const workspace = this.workspaces.get(id);
    if (!workspace) {
      const configuredRoot = this.config.allowedRoots.find((allowedRoot) => workspaceIdForRoot(allowedRoot) === id);
      if (configuredRoot) return this.openWorkspace(configuredRoot, { select: false });
    }
    if (!workspace) {
      throw new CodexProError(`Unknown workspace_id: ${id}. Call open_workspace first.`);
    }
    return workspace;
  }

  listWorkspaces(): Workspace[] {
    return [...this.workspaces.values()];
  }

  currentWorkspaceId(): string {
    return this.getWorkspace().id;
  }
}

export class PathGuard {
  constructor(private readonly config: CodexProConfig) {}

  isBlockedRelativePath(relPath: string): boolean {
    const rel = normalizeRelPath(relPath).replace(/^\.\//, "");
    if (!rel || rel === ".") return false;
    return this.config.blockedGlobs.some((glob) =>
      minimatch(rel, glob, { dot: true, nocase: false, matchBase: false }) ||
      minimatch(path.basename(rel), glob, { dot: true, nocase: false, matchBase: true })
    );
  }

  assertNotBlocked(relPath: string): void {
    if (this.isBlockedRelativePath(relPath)) {
      throw new CodexProError(`Path is blocked by safety rules: ${relPath}`);
    }
  }

  resolve(workspace: Workspace, inputPath = ".", options: { forWrite?: boolean } = {}): { absPath: string; relPath: string } {
    const expanded = expandHome(inputPath || ".");
    const candidate = path.isAbsolute(expanded) ? expanded : path.join(workspace.root, expanded);
    let absPath = path.resolve(candidate);
    const realTarget = maybeRealpath(absPath);
    let relPath = displayPath(absPath, workspace.root);

    if (!this.config.systemAccess && !isSubpath(absPath, workspace.root)) {
      if (realTarget && isSubpath(realTarget, workspace.root)) {
        absPath = realTarget;
        relPath = displayPath(realTarget, workspace.root);
      } else if (options.forWrite) {
        const parent = closestExistingParent(path.dirname(absPath));
        const realParent = maybeRealpath(parent);
        if (!realParent || !isSubpath(realParent, workspace.root)) {
          throw new CodexProError(`Path escapes workspace root: ${inputPath}`);
        }
        absPath = path.resolve(realParent, path.relative(parent, absPath));
        relPath = displayPath(absPath, workspace.root);
      } else {
        throw new CodexProError(`Path escapes workspace root: ${inputPath}`);
      }
    } else if (this.config.systemAccess) {
      // Full-system mode treats the workspace as a default cwd/context, not a
      // filesystem security boundary.  Still canonicalize existing targets and
      // the nearest existing parent so blocked-glob and symlink checks see the
      // real destination rather than a textual alias.
      if (realTarget) {
        absPath = realTarget;
        relPath = displayPath(realTarget, workspace.root);
      } else if (options.forWrite) {
        const parent = closestExistingParent(path.dirname(absPath));
        const realParent = maybeRealpath(parent);
        if (realParent) {
          absPath = path.resolve(realParent, path.relative(parent, absPath));
          relPath = displayPath(absPath, workspace.root);
        }
      }
    }

    this.assertNotBlocked(relPath);

    if (realTarget) {
      if (!this.config.systemAccess && !isSubpath(realTarget, workspace.root)) {
        throw new CodexProError(`Path resolves outside workspace root through a symlink: ${inputPath}`);
      }
      const realRel = displayPath(realTarget, workspace.root);
      this.assertNotBlocked(realRel);
    }

    if (options.forWrite) {
      try {
        if (fs.lstatSync(absPath).isSymbolicLink()) {
          throw new CodexProError(`Refusing to write through a symlink: ${inputPath}`);
        }
      } catch (error) {
        if (error instanceof CodexProError) throw error;
      }
      const parent = closestExistingParent(path.dirname(absPath));
      const realParent = maybeRealpath(parent);
      if (realParent && !this.config.systemAccess && !isSubpath(realParent, workspace.root)) {
        throw new CodexProError(`Write path resolves through a parent outside the workspace: ${inputPath}`);
      }
      if (realParent) {
        const realParentRel = displayPath(realParent, workspace.root);
        this.assertNotBlocked(realParentRel);
      }
    }

    return { absPath, relPath };
  }

  async assertTextFile(absPath: string, maxBytes: number): Promise<void> {
    const stat = await fsp.stat(absPath);
    if (!stat.isFile()) {
      throw new CodexProError(`Not a file: ${absPath}`);
    }
    if (stat.size > maxBytes) {
      throw new CodexProError(`File is too large (${stat.size} bytes). Limit: ${maxBytes} bytes.`);
    }
    if (stat.size === 0) return;
    const handle = await fsp.open(absPath, "r");
    try {
      const sample = Buffer.alloc(Math.min(64 * 1024, stat.size));
      let offset = 0;
      while (offset < stat.size) {
        const { bytesRead } = await handle.read(sample, 0, sample.length, offset);
        if (bytesRead === 0) break;
        if (sample.subarray(0, bytesRead).includes(0)) {
          throw new CodexProError("Refusing to read binary file.");
        }
        offset += bytesRead;
      }
    } finally {
      await handle.close();
    }
  }
}

export function userHome(): string {
  return os.homedir();
}
