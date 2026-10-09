import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';

// This fixture is independent of the denied BiliMate payloads. No production
// file, original rejected request, credential or Git push is replayed.
const root = path.resolve(process.env.SECRET_DIAGNOSTIC_ROOT);
await fs.mkdir(root, { recursive: true });
const child = spawn(process.execPath, ['dist/stdio.js', '--root', root, '--allow-root', root, '--tool-mode', 'full'], {
  cwd: path.resolve('.'),
  env: { ...process.env, CODEXPRO_ROOT: root, CODEXPRO_ALLOWED_ROOTS: root,
    CODEXPRO_WRITE_MODE: 'workspace', CODEXPRO_TOOL_MODE: 'full', CODEXPRO_BASH_MODE: 'off' },
  stdio: ['pipe', 'pipe', 'pipe']
});
let buffer = '';
let nextId = 1;
const pending = new Map();
child.stdout.on('data', chunk => {
  buffer += String(chunk);
  while (buffer.includes('\n')) {
    const end = buffer.indexOf('\n');
    const line = buffer.slice(0, end).trim();
    buffer = buffer.slice(end + 1);
    if (!line) continue;
    const message = JSON.parse(line);
    const item = pending.get(message.id);
    if (!item) continue;
    clearTimeout(item.timer);
    pending.delete(message.id);
    if (message.error) item.reject(new Error(message.error.message));
    else item.resolve(message.result);
  }
});
child.on('exit', code => {
  for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error(`server exited ${code}`)); }
  pending.clear();
});
function request(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}
let ws;
const call = (name, args) => request('tools/call', { name, arguments: { workspace_id: ws, ...args } });
const value = 'synthetic-credential-for-test-only';
const content = `# 合成测试\r\napi_key = "${value}"\r\n`;
function rejected(result, operation, ruleId = 'secret_assignment', line = 2, column = 1) {
  assert.equal(result.isError, true);
  assert.deepEqual(result.structuredContent.content_check, {
    layer: 'codexpro/content_check', code: 'SECRET_CONTENT_BLOCKED', operation,
    ruleId, inputLine: line, inputColumn: column, occurredBeforeMutation: true
  });
  assert.match(result.content[0].text, /Secret-looking content is blocked/);
  assert.match(result.content[0].text, /codexpro\/content_check/);
  assert.equal(JSON.stringify(result).includes(value), false);
}
try {
  await request('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'secret-diagnostic-smoke', version: '1' } });
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  ws = (await request('tools/call', { name: 'open_workspace', arguments: { root, include_tree: false } })).structuredContent.workspace_id;
  assert.ok(ws);
  rejected(await call('write', { path: 'blocked.txt', content }), 'write');
  assert.equal(await fs.stat(path.join(root, 'blocked.txt')).then(() => true, () => false), false);
  assert.notEqual((await call('write', { path: 'ordinary.py', content: 'print("fixture")\n' })).isError, true);
  assert.notEqual((await call('edit', { path: 'ordinary.py', old_text: 'fixture', new_text: 'changed' })).isError, true);
  assert.equal(await fs.readFile(path.join(root, 'ordinary.py'), 'utf8'), 'print("changed")\n');
  rejected(await call('edit', { path: 'ordinary.py', old_text: 'print("changed")\n', new_text: content }), 'edit');
  assert.equal(await fs.readFile(path.join(root, 'ordinary.py'), 'utf8'), 'print("changed")\n');
  const patch = `--- /dev/null\n+++ b/blocked-patch.txt\n@@ -0,0 +1 @@\n+api_key = "${value}"\n`;
  rejected(await call('apply_patch', { patch }), 'apply_patch', 'secret_assignment', 4, 2);
  assert.equal(await fs.stat(path.join(root, 'blocked-patch.txt')).then(() => true, () => false), false);
  const wrapped = await request('tools/call', { name: 'codexpro', arguments: { action: 'write', args: { workspace_id: ws, path: 'blocked-wrapper.txt', content } } });
  rejected(wrapped, 'write');
  assert.equal(await fs.stat(path.join(root, 'blocked-wrapper.txt')).then(() => true, () => false), false);
  const { hasSecretValue, inspectSecretContent, redactSensitiveText } = await import('../dist/redact.js');
  const cases = [
    ['openai_secret', 'sk-' + 'abcdefghijklmno'],
    ['common_token', 'ghp_' + 'abcdefghijklmnopqrstuvwxyz'],
    ['bearer_token', 'Authorization: Bearer ' + value],
    ['cli_token', '--token ' + value],
    ['query_token', '?token=' + value],
    ['codexpro_token_assignment', 'codexpro_token=' + value],
    ['codexpro_token_field', '"codexpro_token": "' + value + '"'],
    ['secret_assignment', 'api_key = "' + value + '"'],
    ['secret_field', '"api_key": "' + value + '"']
  ];
  for (const [rule, text] of cases) {
    assert.equal(hasSecretValue(text), true);
    assert.equal(inspectSecretContent(text).ruleId, rule);
    assert.equal(hasSecretValue(text), true); // repeat must reset global RegExp state
    assert.equal(JSON.stringify(inspectSecretContent(text)).includes(value), false);
    assert.match(redactSensitiveText(text), /REDACTED_SECRET/);
  }
  for (const text of ['print("hello")', 'api_key = process.env.SERVICE_KEY', 'api_key = "[REDACTED_SECRET]"']) {
    assert.equal(hasSecretValue(text), false);
    assert.equal(inspectSecretContent(text), undefined);
  }
  console.log('PASS: write/edit/patch/wrapper rejection metadata, no mutation/no leak, ordinary create/edit, 9 detector families and placeholder compatibility');
} finally {
  child.kill();
}
