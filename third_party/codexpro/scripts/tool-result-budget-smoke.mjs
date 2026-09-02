import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { summarizeLongRun } from "../dist/longRunOps.js";
import { longRunDetailRetrievalHint } from "../dist/detailRetrieval.js";

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

function waitForListening(child) {
  return new Promise((resolve, reject) => {
    let stderr = "";
    const timer = setTimeout(() => reject(new Error(`timeout waiting for HTTP server\n${stderr}`)), 15_000);
    timer.unref();
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
      if (stderr.includes("HTTP MCP listening")) {
        clearTimeout(timer);
        resolve();
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`HTTP server exited before listening: ${code}\n${stderr}`));
    });
  });
}

async function callTool(client, name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  if (result.isError) {
    const text = result.content?.find?.((part) => part.type === "text")?.text ?? JSON.stringify(result.structuredContent);
    throw new Error(`${name} failed: ${text}`);
  }
  return result;
}

const root = await fs.mkdtemp(path.join(os.tmpdir(), "codexpro-tool-result-budget-"));
const port = await getFreePort();
const token = randomBytes(32).toString("base64url");
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
    CODEXPRO_SYSTEM_ACCESS: "0"
  },
  stdio: ["ignore", "pipe", "pipe"]
});

const client = new Client({ name: "tool-result-budget-smoke", version: "0.0.0" });
const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
  requestInit: { headers: { Authorization: `Bearer ${token}` } }
});

const failures = [];
try {
  await waitForListening(child);
  await client.connect(transport);

  const taskIds = [];
  let terminalResult = null;
  let activeTaskId = null;
  for (let index = 0; index < 12; index += 1) {
    const marker = `TASK_BUDGET_${index}_`;
    const started = await callTool(client, "bash", {
      command: `node -e "console.log('${marker}' + 'Z'.repeat(20000))"`
    });
    const taskId = started.structuredContent?.task_id;
    assert.equal(typeof taskId, "string", "bash must return task_id");
    taskIds.push(taskId);
    terminalResult = await callTool(client, "wait_task", { task_id: taskId, wait_seconds: 5 });
    assert.equal(terminalResult.structuredContent?.task?.status, "completed");
  }

  const activeStarted = await callTool(client, "bash", {
    command: "node -e \"setTimeout(() => console.log('ACTIVE_DONE'), 30000)\""
  });
  activeTaskId = activeStarted.structuredContent?.task_id;
  assert.equal(typeof activeTaskId, "string", "active task must return task_id");
  taskIds.push(activeTaskId);

  for (let index = 0; index < 21; index += 1) {
    const started = await callTool(client, "bash", {
      command: `node -e "console.log('RECENT_${index}')"`
    });
    const taskId = started.structuredContent?.task_id;
    assert.equal(typeof taskId, "string");
    taskIds.push(taskId);
    const completed = await callTool(client, "wait_task", { task_id: taskId, wait_seconds: 5 });
    assert.equal(completed.structuredContent?.task?.status, "completed");
  }

  const longCommand = `node -e "console.log('COMMAND_OK')"${" ".repeat(5_000)}`;
  const longCommandStarted = await callTool(client, "bash", { command: longCommand });
  const longCommandTaskId = longCommandStarted.structuredContent?.task_id;
  assert.equal(typeof longCommandTaskId, "string");
  taskIds.push(longCommandTaskId);
  const initialCommandBytes = Buffer.byteLength(String(longCommandStarted.structuredContent?.task?.command ?? ""), "utf8");
  if (initialCommandBytes > 2_000) {
    failures.push(`bash structured command is ${initialCommandBytes} bytes; expected <= 2000`);
  }
  await callTool(client, "wait_task", { task_id: longCommandTaskId, wait_seconds: 5 });
  const longCommandDetail = await callTool(client, "get_task", { task_id: longCommandTaskId });
  const detailCommandBytes = Buffer.byteLength(String(longCommandDetail.structuredContent?.task?.command ?? ""), "utf8");
  if (detailCommandBytes > 2_000) {
    failures.push(`get_task structured command is ${detailCommandBytes} bytes; expected <= 2000`);
  }

  const terminalTask = terminalResult?.structuredContent?.task ?? {};
  const terminalStdoutBytes = Buffer.byteLength(String(terminalTask.stdout ?? ""), "utf8");
  if (terminalStdoutBytes > 8_192) {
    failures.push(`terminal wait_task structured stdout is ${terminalStdoutBytes} bytes; expected <= 8192`);
  }
  if (terminalStdoutBytes > 0 && !Number.isFinite(Number(terminalTask.transportStdoutOmittedBytes))) {
    failures.push("terminal wait_task must report transportStdoutOmittedBytes when structured output is tailed");
  }

  const detail = await callTool(client, "get_task", { task_id: taskIds.at(-1) });
  const detailTask = detail.structuredContent?.task ?? {};
  const detailStdoutBytes = Buffer.byteLength(String(detailTask.stdout ?? ""), "utf8");
  if (detailStdoutBytes > 8_192) {
    failures.push(`get_task structured stdout is ${detailStdoutBytes} bytes; expected <= 8192`);
  }

  const listed = await callTool(client, "list_tasks");
  const listedBytes = Buffer.byteLength(JSON.stringify(listed), "utf8");
  const structured = listed.structuredContent ?? {};
  if (listedBytes > 60_000) {
    failures.push(`list_tasks MCP result is ${listedBytes} bytes; expected <= 60000`);
  }
  if (structured.compacted !== true) {
    failures.push("list_tasks structuredContent must declare compacted=true");
  }
  if (structured.total_count !== taskIds.length) {
    failures.push(`list_tasks total_count=${structured.total_count}; expected ${taskIds.length}`);
  }
  if (!Array.isArray(structured.tasks) || structured.tasks.length > 20) {
    failures.push("list_tasks must return a bounded task summary array (<=20 items)");
  }
  if (!structured.tasks?.some?.((task) => task.task_id === activeTaskId)) {
    failures.push("list_tasks must retain every active task even when newer terminal history exceeds the display budget");
  }
  for (const task of structured.tasks ?? []) {
    if ("stdout" in task || "stderr" in task) failures.push("list_tasks summaries must not contain stdout/stderr");
    if (Buffer.byteLength(String(task.command_preview ?? ""), "utf8") > 512) {
      failures.push("list_tasks command_preview must stay bounded");
    }
  }

  if (activeTaskId) {
    await callTool(client, "cancel_task", { task_id: activeTaskId });
    const activeTerminal = await callTool(client, "wait_task", { task_id: activeTaskId, wait_seconds: 5 });
    assert.equal(activeTerminal.structuredContent?.task?.status, "cancelled");
  }

  const longRunState = {
    runId: "lr_tool_result_budget",
    title: "tool-result budget synthetic run",
    objective: "目".repeat(4_000),
    status: "working",
    workRevision: 42,
    planRevision: 3,
    reviewRound: 2,
    reviews: [{ verdict: "pass", summary: "审".repeat(4_000), workRevision: 42, reviewedAt: new Date().toISOString() }],
    steps: Array.from({ length: 8 }, (_, index) => ({
      id: `s${index + 1}`,
      title: `step-${index + 1}`,
      acceptanceCriteria: Array.from({ length: 5 }, () => "准".repeat(1_000)),
      status: "done",
      evidence: Array.from({ length: 8 }, () => "证".repeat(2_000)),
      notes: Array.from({ length: 4 }, () => "注".repeat(2_000)),
      updatedAt: new Date().toISOString()
    })),
    overallAcceptanceCriteria: Array.from({ length: 10 }, () => "总".repeat(1_000)),
    taskIds: Array.from({ length: 100 }, (_, index) => `task-${index}`),
    taskResolutions: Object.fromEntries(Array.from({ length: 100 }, (_, index) => [
      `task-${index}`,
      { status: "completed", evidence: "终".repeat(1_000), resolvedAt: new Date().toISOString() }
    ])),
    checkpoints: Array.from({ length: 20 }, (_, index) => ({
      at: new Date().toISOString(),
      type: "progress",
      message: `checkpoint-${index}-${"进".repeat(1_000)}`
    })),
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    completion: null
  };
  const longRunObservations = Array.from({ length: 100 }, (_, index) => ({
    taskId: `task-${index}`,
    status: "completed",
    detail: "详".repeat(1_000)
  }));
  const longRunSummary = {
    ...summarizeLongRun(longRunState, longRunObservations),
    detail_retrieval: longRunDetailRetrievalHint(longRunState)
  };
  const longRunSummaryBytes = Buffer.byteLength(JSON.stringify(longRunSummary), "utf8");
  if (longRunSummaryBytes > 60_000) {
    failures.push(`summarizeLongRun transport is ${longRunSummaryBytes} bytes; expected <= 60000`);
  }
  if (longRunSummary.task_id_count !== 100 || longRunSummary.task_resolution_count !== 100) {
    failures.push("summarizeLongRun must preserve total task/resolution counts while compacting transport tails");
  }

  if (failures.length) {
    throw new Error(`tool-result budget regression:\n- ${failures.join("\n- ")}`);
  }
  console.log(`tool-result-budget-smoke: ok list_tasks_bytes=${listedBytes} long_run_summary_bytes=${longRunSummaryBytes} tasks=${taskIds.length}`);
} finally {
  await client.close().catch(() => {});
  child.kill("SIGTERM");
  await new Promise((resolve) => setTimeout(resolve, 250));
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
