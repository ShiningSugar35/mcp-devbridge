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

function startServer(root, port, token) {
  return spawn("node", ["dist/http.js"], {
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
}

async function connectClient(port, token, name) {
  const client = new Client({ name, version: "0.0.0" });
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
  });
  await client.connect(transport);
  return client;
}

async function callTool(client, name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  if (result.isError) {
    const text = result.content?.find?.((part) => part.type === "text")?.text ?? JSON.stringify(result.structuredContent);
    throw new Error(`${name} failed: ${text}`);
  }
  return result;
}

const root = await fs.mkdtemp(path.join(os.tmpdir(), "codexpro-durable-restart-smoke-"));
const token = createHash("sha256").update(`${root}:durable-restart-smoke`).digest("hex");
let firstServer;
let secondServer;
let firstClient;
let secondClient;

try {
  const firstPort = await getFreePort();
  firstServer = startServer(root, firstPort, token);
  await waitForListening(firstServer);
  firstClient = await connectClient(firstPort, token, "durable-task-restart-smoke-first");

  const startedRun = await callTool(firstClient, "long_run_start", {
    title: "durable task restart smoke",
    objective: "prove an unobserved task terminal receipt survives a CodexPro process restart",
    steps: [{ title: "run one task", acceptance_criteria: ["task terminal state is durable"] }],
  });
  const runId = startedRun.structuredContent?.run_id;
  assert.match(runId, /^lr_/);

  const startedTask = await callTool(firstClient, "bash", {
    command: 'node -e "setTimeout(() => process.exit(0), 100)"',
    long_run_id: runId,
    long_run_step_id: "s1",
  });
  const taskId = startedTask.structuredContent?.task_id;
  assert.ok(taskId);

  // Deliberately never call get_task, wait_task, list_tasks, or long_run_status
  // before replacing the CodexPro process. The terminal callback must persist on
  // its own, not because an observer happened to touch the in-memory task.
  await sleep(1_500);
  await firstClient.close();
  firstClient = undefined;
  firstServer.kill("SIGTERM");
  await waitForExit(firstServer);
  firstServer = undefined;

  const secondPort = await getFreePort();
  secondServer = startServer(root, secondPort, token);
  await waitForListening(secondServer);
  secondClient = await connectClient(secondPort, token, "durable-task-restart-smoke-second");

  const recovered = await callTool(secondClient, "long_run_status", { run_id: runId });
  const resolution = recovered.structuredContent?.task_resolutions?.[taskId];
  assert.equal(resolution?.status, "completed");
  assert.match(resolution?.evidence ?? "", /terminal receipt/i);
  assert.equal(
    (recovered.structuredContent?.task_observations ?? []).find((item) => item.taskId === taskId)?.status,
    "completed",
  );
  assert.equal(
    (recovered.structuredContent?.completion_blockers ?? []).some((item) => item.includes(taskId) && /unknown|running|cancelling/i.test(item)),
    false,
  );

  console.log("durable-task-restart-smoke: ok");
} finally {
  if (firstClient) await firstClient.close().catch(() => undefined);
  if (secondClient) await secondClient.close().catch(() => undefined);
  if (firstServer) {
    firstServer.kill("SIGTERM");
    await waitForExit(firstServer);
  }
  if (secondServer) {
    secondServer.kill("SIGTERM");
    await waitForExit(secondServer);
  }
  await removeTreeBestEffort(root);
}
