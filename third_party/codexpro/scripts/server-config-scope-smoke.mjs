import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

function encode(message) {
  return `${JSON.stringify(message)}\n`;
}

class Client {
  constructor(command, args, options) {
    this.child = spawn(command, args, options);
    this.buffer = '';
    this.nextId = 1;
    this.pending = new Map();
    this.child.stdout.on('data', (chunk) => this.onData(String(chunk)));
    this.child.stderr.on('data', (chunk) => process.stderr.write(chunk));
    this.child.on('exit', (code) => {
      for (const { reject } of this.pending.values()) reject(new Error(`server exited ${code}`));
    });
  }

  onData(chunk) {
    this.buffer += chunk;
    while (true) {
      const index = this.buffer.indexOf('\n');
      if (index < 0) return;
      const line = this.buffer.slice(0, index).replace(/\r$/, '');
      this.buffer = this.buffer.slice(index + 1);
      if (!line.trim()) continue;
      const message = JSON.parse(line);
      if (!message.id || !this.pending.has(message.id)) continue;
      const { resolve, reject, timer } = this.pending.get(message.id);
      clearTimeout(timer);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    }
  }

  request(method, params) {
    const id = this.nextId++;
    this.child.stdin.write(encode({ jsonrpc: '2.0', id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`timeout waiting for ${method}`)), 15000);
      timer.unref();
      this.pending.set(id, { resolve, reject, timer });
    });
  }

  notify(method, params = {}) {
    this.child.stdin.write(encode({ jsonrpc: '2.0', method, params }));
  }

  close() {
    this.child.kill('SIGTERM');
  }
}

const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-config-scope-'));
const client = new Client(process.execPath, ['dist/stdio.js', '--root', root, '--allow-root', root, '--tool-mode', 'full'], {
  cwd: path.resolve('.'),
  env: { ...process.env, CODEXPRO_ROOT: root, CODEXPRO_ALLOWED_ROOTS: root },
});

try {
  await client.request('initialize', {
    protocolVersion: '2025-11-25',
    capabilities: {},
    clientInfo: { name: 'server-config-scope-smoke', version: '0.1.0' },
  });
  client.notify('notifications/initialized');
  const result = await client.request('tools/call', { name: 'server_config', arguments: {} });
  const config = result.structuredContent ?? {};
  if (config.registeredToolScope !== 'codexpro_project_engine') {
    throw new Error(`server_config registeredToolScope is ambiguous: ${JSON.stringify(config)}`);
  }
  if (config.registeredToolCountIncludesGatewayTools !== false) {
    throw new Error(`server_config must state that Gateway tools are excluded: ${JSON.stringify(config)}`);
  }
  if (!String(config.registeredToolCountNote ?? '').includes('Hub tools/list')) {
    throw new Error(`server_config must direct public comparisons to Hub tools/list: ${JSON.stringify(config)}`);
  }
  console.log('✓ server_config tool-count scope smoke test passed');
} finally {
  client.close();
  await fs.rm(root, { recursive: true, force: true });
}
