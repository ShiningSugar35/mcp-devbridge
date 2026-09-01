import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import test from "node:test";

import { JsonLineRpcServer } from "../src/stdioServer.js";

async function runLines(lines: string[], handler: (method: string, params: unknown) => Promise<unknown>) {
  const input = new PassThrough();
  const output = new PassThrough();
  let text = "";
  output.setEncoding("utf8");
  output.on("data", (chunk) => { text += chunk; });
  const server = new JsonLineRpcServer(input, output, handler, 512);
  const running = server.run();
  for (const line of lines) input.write(`${line}\n`);
  input.end();
  await running;
  return text.trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line) as Record<string, unknown>);
}

test("stdio JSON-RPC preserves request ordering and isolates handler errors", async () => {
  const seen: string[] = [];
  const responses = await runLines([
    JSON.stringify({ id: 1, method: "first", params: { delay: 10 } }),
    JSON.stringify({ id: 2, method: "boom" }),
    JSON.stringify({ id: 3, method: "third" }),
  ], async (method) => {
    seen.push(method);
    if (method === "first") await new Promise((resolve) => setTimeout(resolve, 10));
    if (method === "boom") throw new Error("expected failure");
    return { method };
  });
  assert.deepEqual(seen, ["first", "boom", "third"]);
  assert.deepEqual(responses.map((item) => item.id), [1, 2, 3]);
  assert.match(JSON.stringify(responses[1]), /expected failure/);
});

test("malformed and oversized lines fail closed without invoking the handler", async () => {
  let calls = 0;
  const responses = await runLines([
    "{broken",
    JSON.stringify({ id: 4, method: "x", params: { value: "z".repeat(800) } }),
  ], async () => { calls += 1; return {}; });
  assert.equal(calls, 0);
  assert.equal(responses.length, 2);
  assert.match(JSON.stringify(responses[0]), /-32700/);
  assert.match(JSON.stringify(responses[1]), /line limit/);
});
