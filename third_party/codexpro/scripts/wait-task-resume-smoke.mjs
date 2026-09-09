import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const project = path.resolve(here, '..');
const fixture = fs.mkdtempSync(path.join(project, '.wait-task-resume-'));
const { loadConfig } = await import(pathToFileURL(path.join(project, 'dist/config.js')).href);
const { createCodexProServer } = await import(pathToFileURL(path.join(project, 'dist/server.js')).href);
const config = loadConfig(['--root', fixture, '--bash', 'full', '--write', 'off']);
config.toolCards = false;
config.analysisEnabled = false;
config.toolMode = 'full';
const handlers = new Map();
let server;
let prototype;
let original;
try {
  server = createCodexProServer(config);
  prototype = Object.getPrototypeOf(server);
  original = prototype.registerTool;
  await server.close();
  server = undefined;
  prototype.registerTool = function(name, options, handler) {
    handlers.set(name, handler);
    return original.call(this, name, options, handler);
  };
  server = createCodexProServer(config);
  prototype.registerTool = original;
  const bash = handlers.get('bash');
  const waitTask = handlers.get('wait_task');
  assert.equal(typeof bash, 'function');
  assert.equal(typeof waitTask, 'function');
  const started = await bash({ command: 'pwd' }, { mcpReq: {} });
  const taskId = started.structuredContent?.task_id;
  assert.ok(taskId, 'bash must return a task id');
  const completed = await waitTask({ task_id: taskId, wait_seconds: 5 }, { mcpReq: {} });
  assert.equal(completed.structuredContent?.task?.status, 'completed');
  const noProgress = await waitTask({ task_id: taskId, wait_seconds: 120 }, { mcpReq: {} });
  assert.equal(noProgress.structuredContent?.progress_liveness_active, false);
  assert.equal(
    noProgress.structuredContent?.effective_wait_seconds,
    15,
    'a client without MCP progress must not hold one silent wait_task request for 30 seconds'
  );
  console.log('wait-task resume smoke passed');
} finally {
  if (prototype && original) prototype.registerTool = original;
  if (server) await server.close();
  fs.rmSync(fixture, { recursive: true, force: true });
}
