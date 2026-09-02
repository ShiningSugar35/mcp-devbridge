import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";

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

async function callToolRaw(client, name, args = {}) {
  return client.callTool({ name, arguments: args });
}

async function callTool(client, name, args = {}) {
  const result = await callToolRaw(client, name, args);
  if (result.isError) {
    const text = result.content?.find?.((part) => part.type === "text")?.text ?? JSON.stringify(result.structuredContent);
    throw new Error(`${name} failed: ${text}`);
  }
  return result;
}

async function callSuper(client, action, args = {}) {
  return callTool(client, "codexpro", { action, args });
}

async function expectSuperError(client, action, args, pattern) {
  const result = await callToolRaw(client, "codexpro", { action, args });
  assert.equal(result.isError, true, `${action} should fail`);
  const text = result.content?.find?.((part) => part.type === "text")?.text ?? JSON.stringify(result.structuredContent);
  assert.match(String(text), pattern);
}

function resultBytes(result) {
  return Buffer.byteLength(JSON.stringify(result), "utf8");
}

const root = await fs.mkdtemp(path.join(os.tmpdir(), "codexpro-detail-retrieval-"));
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
    CODEXPRO_SYSTEM_ACCESS: "0",
    CODEXPRO_MAX_OUTPUT_BYTES: "120000"
  },
  stdio: ["ignore", "pipe", "pipe"]
});

const client = new Client({ name: "detail-retrieval-smoke", version: "0.0.0" });
const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
  requestInit: { headers: { Authorization: `Bearer ${token}` } }
});

try {
  await waitForListening(child);
  await client.connect(transport);

  const expected = `DETAIL_HEAD|${"中🙂XYZ".repeat(7000)}|DETAIL_TAIL\n`;
  const started = await callTool(client, "bash", {
    command: `node -e "process.stdout.write('DETAIL_HEAD|' + '中🙂XYZ'.repeat(7000) + '|DETAIL_TAIL\\n')"`
  });
  const taskId = started.structuredContent?.task_id;
  assert.equal(typeof taskId, "string");
  const terminal = await callTool(client, "wait_task", { task_id: taskId, wait_seconds: 5 });
  assert.equal(terminal.structuredContent?.task?.status, "completed");
  assert.ok(Number(terminal.structuredContent?.task?.transportStdoutOmittedBytes) > 0, "compact terminal result must omit bytes");
  assert.equal(terminal.structuredContent?.task?.detail_retrieval?.action, "task_output");

  let cursor = null;
  let reconstructed = "";
  let pages = 0;
  let firstTaskCursor = null;
  do {
    const page = await callSuper(client, "task_output", {
      task_id: taskId,
      stream: "stdout",
      cursor,
      limit_bytes: 8192
    });
    assert.ok(resultBytes(page) < 40_000, `task_output page too large: ${resultBytes(page)}`);
    const detail = page.structuredContent ?? {};
    assert.equal(detail.task_id, taskId);
    assert.equal(detail.stream, "stdout");
    assert.equal(detail.snapshot_stable, true);
    assert.equal(detail.earlier_output_unavailable, false);
    reconstructed += String(detail.text ?? "");
    if (pages === 0) firstTaskCursor = detail.next_cursor;
    cursor = detail.next_cursor ?? null;
    pages += 1;
    assert.ok(pages < 30, "task output pagination did not terminate");
  } while (cursor);
  assert.equal(reconstructed, expected, "task_output pages must reconstruct the retained redacted buffer exactly");
  assert.ok(pages > 1, "fixture must require multiple task output pages");
  const maxTaskPage = await callSuper(client, "task_output", {
    task_id: taskId,
    stream: "stdout",
    limit_bytes: 16384
  });
  assert.ok(resultBytes(maxTaskPage) < 40_000, `max task_output page too large: ${resultBytes(maxTaskPage)}`);
  assert.equal(typeof firstTaskCursor, "string");
  const tamperedTaskCursor = `${firstTaskCursor.slice(0, -1)}${firstTaskCursor.endsWith("A") ? "B" : "A"}`;
  await expectSuperError(
    client,
    "task_output",
    { task_id: taskId, stream: "stdout", cursor: tamperedTaskCursor, limit_bytes: 8192 },
    /cursor|signature|invalid/i
  );

  const otherStarted = await callTool(client, "bash", { command: "node -e \"console.log('OTHER_TASK')\"" });
  const otherTaskId = otherStarted.structuredContent?.task_id;
  await callTool(client, "wait_task", { task_id: otherTaskId, wait_seconds: 5 });
  await expectSuperError(
    client,
    "task_output",
    { task_id: otherTaskId, stream: "stdout", cursor: firstTaskCursor, limit_bytes: 8192 },
    /cursor|task|invalid/i
  );

  const largeStarted = await callTool(client, "bash", {
    command: `node -e "process.stdout.write('TRUNC_HEAD|' + '界🙂'.repeat(30000) + '|TRUNC_TAIL')"`
  });
  const largeTaskId = largeStarted.structuredContent?.task_id;
  await callTool(client, "wait_task", { task_id: largeTaskId, wait_seconds: 5 });
  const retainedPage = await callSuper(client, "task_output", {
    task_id: largeTaskId,
    stream: "stdout",
    limit_bytes: 8192
  });
  assert.equal(retainedPage.structuredContent?.earlier_output_unavailable, true);
  assert.ok(Number(retainedPage.structuredContent?.observed_total_bytes) > Number(retainedPage.structuredContent?.retained_bytes));
  assert.doesNotMatch(String(retainedPage.structuredContent?.text ?? ""), /�/, "UTF-8 retained page must not begin with a broken code point");

  const changingStarted = await callTool(client, "bash", {
    command: `node -e "process.stdout.write('A'.repeat(20000)); setTimeout(() => process.stdout.write('B'.repeat(20000)), 1200); setTimeout(() => {}, 5000)"`
  });
  const changingTaskId = changingStarted.structuredContent?.task_id;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const observed = await callTool(client, "get_task", { task_id: changingTaskId });
    if (Number(observed.structuredContent?.task?.stdoutObservedBytes) > 4096) break;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const changingFirst = await callSuper(client, "task_output", {
    task_id: changingTaskId,
    stream: "stdout",
    limit_bytes: 4096
  });
  const changingCursor = changingFirst.structuredContent?.next_cursor;
  assert.equal(typeof changingCursor, "string");
  await new Promise((resolve) => setTimeout(resolve, 1800));
  await expectSuperError(
    client,
    "task_output",
    { task_id: changingTaskId, stream: "stdout", cursor: changingCursor, limit_bytes: 4096 },
    /changed|cursor|snapshot/i
  );
  await callTool(client, "cancel_task", { task_id: changingTaskId });
  await callTool(client, "wait_task", { task_id: changingTaskId, wait_seconds: 5 });

  const evidence = Array.from({ length: 12 }, (_, index) => `evidence-${index}-${"证".repeat(120)}`);
  const notes = Array.from({ length: 8 }, (_, index) => `note-${index}-${"注".repeat(120)}`);
  const criteria = Array.from({ length: 12 }, (_, index) => `criterion-${index}-${"准".repeat(120)}`);
  const run = await callTool(client, "long_run_start", {
    title: "detail retrieval run",
    objective: "recover compacted durable evidence on demand",
    steps: [{ title: "deep step", acceptance_criteria: criteria }]
  });
  const runId = run.structuredContent?.run_id;
  assert.equal(typeof runId, "string");
  await callTool(client, "long_run_update", {
    run_id: runId,
    step_id: "s1",
    evidence,
    checkpoint: "checkpoint-0"
  });
  for (let index = 0; index < notes.length; index += 1) {
    await callTool(client, "long_run_update", {
      run_id: runId,
      step_id: "s1",
      note: notes[index],
      checkpoint: `checkpoint-${index + 1}-${"进".repeat(120)}`
    });
  }
  const durableStarted = await callTool(client, "bash", {
    command: "node -e \"console.log('DURABLE_DETAIL_TASK')\"",
    long_run_id: runId,
    long_run_step_id: "s1"
  });
  const durableTaskId = durableStarted.structuredContent?.task_id;
  await callTool(client, "wait_task", { task_id: durableTaskId, wait_seconds: 5 });
  const compactRun = await callTool(client, "long_run_status", { run_id: runId });
  assert.ok(Number(compactRun.structuredContent?.steps?.[0]?.evidence_count) > compactRun.structuredContent?.steps?.[0]?.evidence_tail?.length);
  assert.equal(compactRun.structuredContent?.detail_retrieval?.action, "run_detail");

  async function collectRunDetail(args) {
    const entries = [];
    let detailCursor = null;
    let pageCount = 0;
    do {
      const page = await callSuper(client, "run_detail", { ...args, cursor: detailCursor });
      assert.ok(resultBytes(page) < 40_000, `run_detail page too large: ${resultBytes(page)}`);
      const structured = page.structuredContent ?? {};
      entries.push(...(structured.entries ?? []));
      detailCursor = structured.next_cursor ?? null;
      pageCount += 1;
      assert.ok(pageCount < 30, "run detail pagination did not terminate");
    } while (detailCursor);
    return entries;
  }

  assert.deepEqual(
    await collectRunDetail({ run_id: runId, section: "step", step_id: "s1", field: "evidence", limit_bytes: 7000 }),
    evidence
  );
  assert.deepEqual(
    await collectRunDetail({ run_id: runId, section: "step", step_id: "s1", field: "notes", limit_bytes: 7000 }),
    notes
  );
  assert.deepEqual(
    await collectRunDetail({ run_id: runId, section: "step", step_id: "s1", field: "acceptance_criteria", limit_bytes: 7000 }),
    criteria
  );
  const checkpoints = await collectRunDetail({ run_id: runId, section: "checkpoints", limit_bytes: 7000 });
  assert.ok(checkpoints.some((item) => item.message === "checkpoint-0"), "old checkpoint must be retrievable");
  const resolutions = await collectRunDetail({ run_id: runId, section: "task_resolutions", limit_bytes: 7000 });
  assert.ok(resolutions.some((item) => item.task_id === durableTaskId && item.status === "completed"), "terminal task resolution must be retrievable");
  const maxRunPage = await callSuper(client, "run_detail", {
    run_id: runId,
    section: "step",
    step_id: "s1",
    field: "evidence",
    limit_bytes: 16384
  });
  assert.ok(resultBytes(maxRunPage) < 40_000, `max run_detail page too large: ${resultBytes(maxRunPage)}`);

  const firstCheckpointPage = await callSuper(client, "run_detail", {
    run_id: runId,
    section: "checkpoints",
    limit_bytes: 1024
  });
  const staleRunCursor = firstCheckpointPage.structuredContent?.next_cursor;
  assert.equal(typeof staleRunCursor, "string");
  await callTool(client, "long_run_update", { run_id: runId, step_id: "s1", checkpoint: "checkpoint-after-cursor" });
  await expectSuperError(
    client,
    "run_detail",
    { run_id: runId, section: "checkpoints", cursor: staleRunCursor, limit_bytes: 1024 },
    /changed|cursor|revision|stale/i
  );

  const run2 = await callTool(client, "long_run_start", {
    title: "other detail run",
    objective: "cursor isolation",
    steps: [{ title: "x", acceptance_criteria: ["y"] }]
  });
  await expectSuperError(
    client,
    "run_detail",
    { run_id: run2.structuredContent?.run_id, section: "checkpoints", cursor: firstCheckpointPage.structuredContent?.next_cursor, limit_bytes: 1024 },
    /cursor|run|invalid/i
  );

  const config = await callTool(client, "server_config");
  assert.equal(config.structuredContent?.registeredToolCount, 38, "detail retrieval must not add a public CodexPro tool");
  const actions = await callTool(client, "codexpro", { action: "list_actions" });
  assert.equal(actions.structuredContent?.aliases?.task_output, "get_task");
  assert.equal(actions.structuredContent?.aliases?.run_detail, "long_run_status");

  console.log(`detail-retrieval-smoke: ok task_pages=${pages} checkpoints=${checkpoints.length}`);
} finally {
  await client.close().catch(() => {});
  child.kill("SIGTERM");
  await new Promise((resolve) => setTimeout(resolve, 250));
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
