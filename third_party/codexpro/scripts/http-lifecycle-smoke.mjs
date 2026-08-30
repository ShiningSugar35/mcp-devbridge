import assert from 'node:assert/strict';

import { createGracefulShutdownController } from '../dist/httpLifecycle.js';

const events = [];
let closeCallback;
let forceCloseCount = 0;
let cleanupCount = 0;
let exitCode = null;

const server = {
  close(callback) {
    events.push('server.close');
    closeCallback = callback;
  },
  closeAllConnections() {
    events.push('server.closeAllConnections');
    forceCloseCount += 1;
  }
};

const controller = createGracefulShutdownController({
  server,
  cleanup: async () => {
    events.push('cleanup');
    cleanupCount += 1;
  },
  timeoutMs: 100,
  logger: (message) => events.push(message),
  exit: (code) => {
    exitCode = code;
  }
});

const first = controller.shutdown('SIGTERM');
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(typeof closeCallback, 'function', 'server must stop accepting new connections');
assert.equal(cleanupCount, 0, 'runtime cleanup must not dispose MCP state before active HTTP/SSE drain completes');
closeCallback();
await first;
assert.equal(exitCode, 0);
assert.equal(cleanupCount, 1, 'runtime cleanup must run exactly once after drain');
assert.equal(forceCloseCount, 0, 'normal drain must not force-close connections');
assert.ok(events.indexOf('server.close') < events.indexOf('cleanup'), 'server drain must precede runtime cleanup');
assert.ok(events.some((item) => String(item).includes('graceful shutdown start')));
assert.ok(events.some((item) => String(item).includes('graceful shutdown complete')));

await controller.shutdown('SIGINT');
assert.equal(cleanupCount, 1, 'repeated signals must be idempotent');

let forcedClose = 0;
let forcedExit = null;
let forcedCleanup = 0;
const timeoutController = createGracefulShutdownController({
  server: {
    close() {},
    closeAllConnections() {
      forcedClose += 1;
    }
  },
  cleanup: async () => {
    forcedCleanup += 1;
  },
  timeoutMs: 20,
  logger: () => {},
  exit: (code) => {
    forcedExit = code;
  }
});
await timeoutController.shutdown('SIGTERM');
assert.equal(forcedClose, 1, 'bounded timeout must force-close lingering connections');
assert.equal(forcedCleanup, 1, 'runtime cleanup must run after the force-close path');
assert.equal(forcedExit, 0);

let cleanupTimeoutExit = null;
let cleanupTimeoutCloseCallback;
const cleanupTimeoutController = createGracefulShutdownController({
  server: {
    close(callback) {
      cleanupTimeoutCloseCallback = callback;
      queueMicrotask(() => callback());
    },
    closeAllConnections() {}
  },
  cleanup: () => new Promise(() => {}),
  timeoutMs: 20,
  logger: () => {},
  exit: (code) => {
    cleanupTimeoutExit = code;
  }
});
await cleanupTimeoutController.shutdown('SIGTERM');
assert.equal(typeof cleanupTimeoutCloseCallback, 'function');
assert.equal(cleanupTimeoutExit, 1, 'a hung runtime cleanup must fail the bounded shutdown instead of hanging indefinitely');

console.log('http-lifecycle-smoke: ok');
