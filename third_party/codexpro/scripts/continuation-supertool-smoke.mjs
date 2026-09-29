import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const project = path.resolve(here, '..');
const fixture = fs.mkdtempSync(path.join(project, '.continuation-supertool-'));
const { loadConfig } = await import(pathToFileURL(path.join(project, 'dist/config.js')).href);
const { createCodexProServer } = await import(pathToFileURL(path.join(project, 'dist/server.js')).href);
const config = loadConfig(['--root', fixture, '--bash', 'off', '--write', 'workspace']);
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

  const supertool = handlers.get('codexpro');
  const start = handlers.get('long_run_start');
  const status = handlers.get('long_run_status');
  assert.equal(typeof supertool, 'function');
  assert.equal(typeof start, 'function');
  assert.equal(typeof status, 'function');

  const started = await start({
    title: 'Continuation fixture',
    objective: 'verify canonical long-run supertool continuation control',
    steps: [{ title: 'fixture', acceptance_criteria: ['pass'] }]
  });
  assert.notEqual(started.isError, true);
  const runId = started.structuredContent.run_id;

  let result = await supertool({
    action: 'long_run_update',
    args: {
      run_id: runId,
      continuation: { operation: 'request_schedule', expected_revision: 0 }
    }
  });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.equal(result.structuredContent.host_action_required, 'ensure_hourly_schedule');
  const requestId = result.structuredContent.scheduler.requestId;

  result = await supertool({
    action: 'long_run_update',
    args: {
      run_id: runId,
      continuation: {
        operation: 'bind_schedule',
        expected_revision: 1,
        request_id: requestId,
        automation_id: 'fixture-hourly',
        cadence_minutes: 60
      }
    }
  });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.equal(result.structuredContent.scheduler.status, 'bound');

  const projected = await status({ run_id: runId });
  assert.equal(projected.structuredContent.continuation.revision, 2);
  assert.equal(projected.structuredContent.continuation.scheduler.status, 'bound');

  const mixed = await supertool({
    action: 'long_run_update',
    args: {
      run_id: runId,
      checkpoint: 'ordinary update cannot mix with continuation',
      continuation: { operation: 'pause', expected_revision: 2 }
    }
  });
  assert.equal(mixed.isError, true);

  const actions = await supertool({ action: 'list_actions', args: {} });
  assert.equal(actions.structuredContent.aliases.continuation_update, undefined);
  assert.equal(actions.structuredContent.aliases.continuation_status, undefined);
  console.log('continuation-supertool-smoke: ok');
} finally {
  if (prototype && original) prototype.registerTool = original;
  if (server) await server.close();
  fs.rmSync(fixture, { recursive: true, force: true });
}
