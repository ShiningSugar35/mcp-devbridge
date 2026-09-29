import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import fs from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';

const root = await fs.mkdtemp(path.join(os.tmpdir(), 'continuation-http-'));
async function exercise(writeMode) {
  const port = await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      server.close(() => resolve(addr.port));
    });
  });
  const credential = randomBytes(32).toString('base64url');
  const child = spawn(process.execPath, ['dist/http.js'], {
    env: { ...process.env, CODEXPRO_ROOT: root, CODEXPRO_ALLOWED_ROOTS: root, CODEXPRO_HOST: '127.0.0.1', CODEXPRO_PORT: String(port),
      CODEXPRO_HTTP_TOKEN: credential, CODEXPRO_WRITE_MODE: writeMode, CODEXPRO_TOOL_MODE: 'full', CODEXPRO_SYSTEM_ACCESS: '0', CODEXPRO_BASH_MODE: 'off' },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  const exited = new Promise(resolve => child.once('exit', resolve));
  const client = new Client({ name: 'continuation-http-fixture', version: '1' });
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), { requestInit: { headers: { Authorization: `Bearer ${credential}` } } });
  try {
    await new Promise((resolve, reject) => {
      let tail = '';
      const timer = setTimeout(() => reject(new Error('fixture listener timeout')), 15000);
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', () => { clearTimeout(timer); reject(new Error('fixture exited before listening')); });
      child.stderr.on('data', chunk => { tail = (tail + chunk.toString()).slice(-4000); if (tail.includes('HTTP MCP listening')) { clearTimeout(timer); resolve(); } });
    });
    await client.connect(transport);
    const opened = await client.callTool({ name: 'open_workspace', arguments: { root, include_tree: false, include_skills: false } });
    assert.notEqual(opened.isError, true);
    const workspace_id = opened.structuredContent.workspace_id;
    const superCall = (action, args) => client.callTool({ name: 'codexpro', arguments: { action, args: { workspace_id, ...args } } });
    if (writeMode === 'off') {
      const refused = await superCall('long_run_update', { run_id: 'lr_fixture_nonexistent', continuation: { operation: 'request_schedule', expected_revision: 0 } });
      assert.equal(refused.isError, true, 'read-only server must refuse continuation writes');
      console.log('continuation-http: write-off denied');
      return;
    }
    const started = await client.callTool({ name: 'long_run_start', arguments: { workspace_id, title: 'HTTP fixture', objective: 'verify real tool dispatch', steps: [{ title: 'fixture', acceptance_criteria: ['pass'] }] } });
    assert.notEqual(started.isError, true);
    const run_id = started.structuredContent.run_id;
    let result = await superCall('long_run_update', { run_id, continuation: { operation: 'request_schedule', expected_revision: 0 } });
    assert.notEqual(result.isError, true, JSON.stringify(result));
    assert.equal(result.structuredContent.host_action_required, 'ensure_hourly_schedule');
    const request_id = result.structuredContent.scheduler.requestId;
    result = await superCall('long_run_update', { run_id, continuation: { operation: 'bind_schedule', expected_revision: 1, request_id, automation_id: 'fixture-hourly-001', cadence_minutes: 60 } });
    assert.notEqual(result.isError, true, JSON.stringify(result));
    assert.equal(result.structuredContent.scheduler.status, 'bound');
    const before = await fs.readFile(path.join(root, '.ai-bridge', 'long-runs', `${run_id}.json`), 'utf8');
    result = await superCall('long_run_status', { run_id });
    assert.equal(result.structuredContent.continuation.revision, 2);
    assert.equal(await fs.readFile(path.join(root, '.ai-bridge', 'long-runs', `${run_id}.json`), 'utf8'), before, 'status must be read-only');
    const bad = await superCall('long_run_update', { run_id, observations: [], continuation: { operation: 'open_window', expected_revision: 2, invocation_id: 'fixture-001', source: 'scheduled' } });
    assert.equal(bad.isError, true, 'client must not supply task liveness observations');
    console.log('continuation-http: request/bind/read-only projection/argument boundary passed');
  } finally {
    await client.close().catch(() => {});
    if (child.exitCode === null) child.kill('SIGTERM');
    await exited;
  }
}
try {
  await exercise('workspace');
  await exercise('off');
} finally { await fs.rm(root, { recursive: true, force: true }); }
