import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { acquireProfileLock, ProfileLockError } from "../src/profileLock.js";

test("a live profile owner rejects a second controller", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "regular-chat-lock-"));
  try {
    const lockPath = path.join(root, "default-managed.lock");
    const first = await acquireProfileLock(lockPath, {
      controllerInstanceId: "controller-a",
      ownerPid: 111,
      pidAlive: () => true,
    });
    await assert.rejects(
      acquireProfileLock(lockPath, {
        controllerInstanceId: "controller-b",
        ownerPid: 222,
        pidAlive: () => true,
      }),
      ProfileLockError,
    );
    await first.release();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("concurrent stale recovery yields exactly one new profile owner", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "regular-chat-concurrent-stale-lock-"));
  try {
    const lockPath = path.join(root, "default-managed.lock");
    const stale = await acquireProfileLock(lockPath, {
      controllerInstanceId: "stale-controller",
      ownerPid: 111,
      pidAlive: () => true,
    });
    const pidAlive = (pid: number) => pid !== 111;
    const results = await Promise.allSettled([
      acquireProfileLock(lockPath, {
        controllerInstanceId: "controller-b",
        ownerPid: 222,
        pidAlive,
      }),
      acquireProfileLock(lockPath, {
        controllerInstanceId: "controller-c",
        ownerPid: 333,
        pidAlive,
      }),
    ]);
    const winners = results.filter((result) => result.status === "fulfilled");
    const losers = results.filter((result) => result.status === "rejected");
    assert.equal(winners.length, 1);
    assert.equal(losers.length, 1);
    assert.match(String((losers[0] as PromiseRejectedResult).reason), /already owned|contended/i);

    await stale.release();
    const payload = JSON.parse(await readFile(lockPath, "utf8")) as Record<string, unknown>;
    assert.ok(payload.owner_pid === 222 || payload.owner_pid === 333);
    await (winners[0] as PromiseFulfilledResult<Awaited<ReturnType<typeof acquireProfileLock>>>).value.release();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("stale lock recovery touches only the DevBridge lock file", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "regular-chat-stale-lock-"));
  try {
    const lockPath = path.join(root, "default-managed.lock");
    const chromiumLock = path.join(root, "SingletonLock");
    await writeFile(chromiumLock, "browser-owned", "utf8");
    const first = await acquireProfileLock(lockPath, {
      controllerInstanceId: "controller-a",
      ownerPid: 111,
      pidAlive: () => true,
    });
    const second = await acquireProfileLock(lockPath, {
      controllerInstanceId: "controller-b",
      ownerPid: 222,
      pidAlive: () => false,
    });
    const payload = JSON.parse(await readFile(lockPath, "utf8")) as Record<string, unknown>;
    assert.deepEqual(Object.keys(payload).sort(), [
      "controller_instance_id",
      "owner_pid",
      "started_at",
    ]);
    assert.equal(payload.controller_instance_id, "controller-b");
    assert.equal(await readFile(chromiumLock, "utf8"), "browser-owned");
    await first.release();
    assert.equal(JSON.parse(await readFile(lockPath, "utf8")).controller_instance_id, "controller-b");
    await second.release();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
