import { mkdir, open, readFile, unlink } from "node:fs/promises";
import path from "node:path";

import { lock as acquireInterprocessLock } from "@bybrave/proper-lockfile2";

import { MAX_PROFILE_LOCK_BYTES } from "./limits.js";

export class ProfileLockError extends Error {}

interface LockPayload {
  owner_pid: number;
  controller_instance_id: string;
  started_at: string;
}

export interface AcquireProfileLockOptions {
  controllerInstanceId: string;
  ownerPid?: number;
  pidAlive?: (pid: number) => boolean;
}

export interface ProfileLockHandle {
  readonly path: string;
  readonly payload: Readonly<LockPayload>;
  release(): Promise<void>;
}

function defaultPidAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return code === "EPERM";
  }
}

async function readPayload(lockPath: string): Promise<LockPayload> {
  const data = await readFile(lockPath);
  if (data.byteLength > MAX_PROFILE_LOCK_BYTES) {
    throw new ProfileLockError("profile lock exceeds size limit");
  }
  let value: unknown;
  try {
    value = JSON.parse(data.toString("utf8"));
  } catch {
    throw new ProfileLockError("profile lock is corrupt");
  }
  if (
    typeof value !== "object" ||
    value === null ||
    !Number.isSafeInteger((value as Record<string, unknown>).owner_pid) ||
    typeof (value as Record<string, unknown>).controller_instance_id !== "string" ||
    typeof (value as Record<string, unknown>).started_at !== "string"
  ) {
    throw new ProfileLockError("profile lock has invalid schema");
  }
  return value as LockPayload;
}

async function createExclusive(lockPath: string, payload: LockPayload): Promise<void> {
  const handle = await open(lockPath, "wx", 0o600);
  try {
    const encoded = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
    if (encoded.byteLength > MAX_PROFILE_LOCK_BYTES) {
      throw new ProfileLockError("profile lock payload exceeds size limit");
    }
    await handle.writeFile(encoded);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function withStaleRecoveryGuard<T>(lockPath: string, operation: () => Promise<T>): Promise<T> {
  let release: (() => Promise<void>) | null = null;
  try {
    release = await acquireInterprocessLock(lockPath, {
      realpath: false,
      lockfilePath: `${lockPath}.recovery`,
      stale: 5_000,
      update: 1_000,
      retries: {
        retries: 10,
        factor: 1.5,
        minTimeout: 20,
        maxTimeout: 100,
        randomize: true,
      },
    });
  } catch (error) {
    throw new ProfileLockError(
      `profile stale-lock recovery is contended: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  try {
    return await operation();
  } finally {
    await release().catch(() => undefined);
  }
}

export async function acquireProfileLock(
  lockPath: string,
  options: AcquireProfileLockOptions,
): Promise<ProfileLockHandle> {
  if (!options.controllerInstanceId) {
    throw new ProfileLockError("controller instance id is required");
  }
  const ownerPid = options.ownerPid ?? process.pid;
  const pidAlive = options.pidAlive ?? defaultPidAlive;
  await mkdir(path.dirname(lockPath), { recursive: true });

  const payload: LockPayload = {
    owner_pid: ownerPid,
    controller_instance_id: options.controllerInstanceId,
    started_at: new Date().toISOString(),
  };

  try {
    await createExclusive(lockPath, payload);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      throw error;
    }
    await withStaleRecoveryGuard(lockPath, async () => {
      let existing: LockPayload | null = null;
      try {
        existing = await readPayload(lockPath);
      } catch (readError) {
        if ((readError as NodeJS.ErrnoException).code !== "ENOENT") {
          throw readError;
        }
      }
      if (existing && pidAlive(existing.owner_pid)) {
        throw new ProfileLockError(
          `profile is already owned by controller ${existing.controller_instance_id}`,
        );
      }
      if (existing) {
        try {
          await unlink(lockPath);
        } catch (unlinkError) {
          if ((unlinkError as NodeJS.ErrnoException).code !== "ENOENT") {
            throw new ProfileLockError("failed to recover stale DevBridge profile lock");
          }
        }
      }
      try {
        await createExclusive(lockPath, payload);
      } catch (retryError) {
        if ((retryError as NodeJS.ErrnoException).code === "EEXIST") {
          throw new ProfileLockError("profile lock ownership changed during stale-lock recovery");
        }
        throw retryError;
      }
    });
  }

  let released = false;
  return {
    path: lockPath,
    payload,
    async release(): Promise<void> {
      if (released) {
        return;
      }
      released = true;
      try {
        const current = await readPayload(lockPath);
        if (
          current.owner_pid !== payload.owner_pid ||
          current.controller_instance_id !== payload.controller_instance_id ||
          current.started_at !== payload.started_at
        ) {
          return;
        }
        await unlink(lockPath);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") {
          return;
        }
        if (error instanceof ProfileLockError) {
          return;
        }
        throw error;
      }
    },
  };
}
