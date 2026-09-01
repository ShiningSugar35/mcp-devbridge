import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : undefined;
      server.close(() => (port ? resolve(port) : reject(new Error("no free port"))));
    });
    server.on("error", reject);
  });
}

async function waitForListening(child, timeoutMs = 15_000) {
  let stderr = "";
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout waiting for HTTP server\n${stderr}`)), timeoutMs);
    timer.unref();
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
      if (stderr.includes("HTTP MCP listening")) {
        clearTimeout(timer);
        resolve();
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`HTTP server exited before listening: ${code}\n${stderr}`));
    });
  });
}

async function waitForExit(child, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve();
    }, timeoutMs);
    timer.unref();
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

async function removeTreeBestEffort(target) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      await fs.rm(target, { recursive: true, force: true });
      return;
    } catch (error) {
      if (!["EBUSY", "EPERM", "ENOTEMPTY"].includes(error?.code) || attempt === 9) return;
      await sleep(100 * (attempt + 1));
    }
  }
}

async function callTool(client, name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  if (result.isError) {
    const text = result.content?.find?.((part) => part.type === "text")?.text ?? JSON.stringify(result.structuredContent);
    throw new Error(`${name} failed: ${text}`);
  }
  return result;
}

async function waitForResolution(client, runId, taskId, expectedStatus, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await callTool(client, "long_run_status", { run_id: runId });
    const resolution = status.structuredContent?.task_resolutions?.[taskId];
    if (resolution) {
      assert.equal(resolution.status, expectedStatus);
      return { status, resolution };
    }
    await sleep(50);
  }
  throw new Error(`timed out waiting for ${expectedStatus} resolution of ${taskId}`);
}

const root = await fs.mkdtemp(path.join(os.tmpdir(), "codexpro-durable-http-smoke-"));
const port = await getFreePort();
const token = createHash("sha256").update(`${root}:${port}:durable-http-smoke`).digest("hex");
const child = spawn("node", ["dist/http.js"], {
  cwd: path.resolve("."),
  env: {
    ...process.env,
    CODEXPRO_ROOT: root,
    CODEXPRO_ALLOWED_ROOTS: root,
    CODEXPRO_HOST: "127.0.0.1",
    CODEXPRO_PORT: String(port),
    CODEXPRO_HTTP_TOKEN: token,
    CODEXPRO_BASH_MODE: "full",
    CODEXPRO_WRITE_MODE: "workspace",
    CODEXPRO_TOOL_MODE: "full",
    CODEXPRO_TOOL_CARDS: "0",
  },
  stdio: ["ignore", "pipe", "pipe"],
});

let client;
try {
  await waitForListening(child);
  client = new Client({ name: "durable-task-http-smoke", version: "0.0.0" });
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
  });
  await client.connect(transport);

  const startedRun = await callTool(client, "long_run_start", {
    title: "HTTP durable task terminal smoke",
    objective: "prove terminal task state is persisted without wait_task",
    steps: [{ title: "run terminal matrix", acceptance_criteria: ["terminal state is durable"] }],
  });
  const runId = startedRun.structuredContent?.run_id;
  assert.match(runId, /^lr_/);

  const completed = await callTool(client, "bash", {
    command: 'node -e "setTimeout(() => process.exit(0), 50)"',
    long_run_id: runId,
    long_run_step_id: "s1",
  });
  const completedId = completed.structuredContent?.task_id;
  assert.ok(completedId);
  const immediateStatus = await callTool(client, "long_run_status", { run_id: runId });
  assert.ok(immediateStatus.structuredContent?.task_ids?.includes(completedId));
  const completedResolution = await waitForResolution(client, runId, completedId, "completed");
  assert.match(completedResolution.resolution.evidence, /terminal receipt/i);

  const beforeFailureIds = new Set(completedResolution.status.structuredContent?.task_ids ?? []);
  let startFailed = false;
  try {
    await callTool(client, "bash", {
      command: 'node -e "process.exit(0)"',
      cwd: "missing-durable-task-cwd",
      long_run_id: runId,
      long_run_step_id: "s1",
    });
  } catch (error) {
    startFailed = /working directory does not exist/i.test(String(error));
  }
  assert.equal(startFailed, true);
  const afterFailure = await callTool(client, "long_run_status", { run_id: runId });
  const newFailureIds = (afterFailure.structuredContent?.task_ids ?? []).filter((taskId) => !beforeFailureIds.has(taskId));
  assert.equal(newFailureIds.length, 1);
  const failedResolution = afterFailure.structuredContent?.task_resolutions?.[newFailureIds[0]];
  assert.equal(failedResolution?.status, "failed");
  assert.match(failedResolution?.evidence ?? "", /failed before process start/i);
  assert.doesNotMatch(failedResolution?.evidence ?? "", /node -e|missing-durable-task-cwd/i);

  const cancellable = await callTool(client, "bash", {
    command: 'node -e "setTimeout(() => process.exit(0), 30000)"',
    long_run_id: runId,
    long_run_step_id: "s1",
  });
  const cancelledId = cancellable.structuredContent?.task_id;
  assert.ok(cancelledId);
  const cancelResult = await callTool(client, "cancel_task", { task_id: cancelledId });
  assert.equal(cancelResult.structuredContent?.task?.status, "cancelling");
  let terminalAfterCancel = null;
  const cancelDeadline = Date.now() + 10_000;
  while (Date.now() < cancelDeadline) {
    const observed = await callTool(client, "get_task", { task_id: cancelledId });
    const task = observed.structuredContent?.task;
    if (["completed", "failed", "cancelled"].includes(task?.status)) {
      terminalAfterCancel = task;
      break;
    }
    await sleep(100);
  }
  if (!terminalAfterCancel) {
    const observed = await callTool(client, "get_task", { task_id: cancelledId });
    throw new Error(`cancelled task did not become terminal: ${JSON.stringify(observed.structuredContent?.task)}`);
  }
  assert.equal(terminalAfterCancel.status, "cancelled");
  await waitForResolution(client, runId, cancelledId, "cancelled");

  console.log("durable-task-http-smoke: ok");
} finally {
  if (client) await client.close().catch(() => undefined);
  child.kill("SIGTERM");
  await waitForExit(child);
  await removeTreeBestEffort(root);
}
