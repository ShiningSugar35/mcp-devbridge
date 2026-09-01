import readline from "node:readline";
import type { Readable, Writable } from "node:stream";

export interface RpcRequest {
  id: number | string;
  method: string;
  params?: unknown;
}

export interface RpcHandler {
  (method: string, params: unknown): Promise<unknown>;
}

export class JsonLineRpcServer {
  readonly #input: Readable;
  readonly #output: Writable;
  readonly #handler: RpcHandler;
  readonly #maxLineBytes: number;
  #closed = false;
  #chain: Promise<void> = Promise.resolve();

  constructor(input: Readable, output: Writable, handler: RpcHandler, maxLineBytes = 1024 * 1024) {
    this.#input = input;
    this.#output = output;
    this.#handler = handler;
    this.#maxLineBytes = maxLineBytes;
  }

  async run(): Promise<void> {
    const rl = readline.createInterface({ input: this.#input, crlfDelay: Infinity });
    try {
      for await (const line of rl) {
        if (this.#closed) break;
        if (!line.trim()) continue;
        this.#chain = this.#chain.then(() => this.#handleLine(line));
        await this.#chain;
      }
    } finally {
      rl.close();
      await this.#chain;
    }
  }

  close(): void {
    this.#closed = true;
  }

  async #handleLine(line: string): Promise<void> {
    if (Buffer.byteLength(line, "utf8") > this.#maxLineBytes) {
      this.#write({ id: null, error: { code: -32010, message: "request exceeds line limit" } });
      return;
    }
    let request: RpcRequest;
    try {
      const parsed = JSON.parse(line) as Partial<RpcRequest>;
      if ((typeof parsed.id !== "number" && typeof parsed.id !== "string") || typeof parsed.method !== "string") {
        throw new Error("invalid request shape");
      }
      request = parsed as RpcRequest;
    } catch (error) {
      this.#write({ id: null, error: { code: -32700, message: (error as Error).message } });
      return;
    }
    try {
      const result = await this.#handler(request.method, request.params ?? {});
      this.#write({ id: request.id, result });
    } catch (error) {
      this.#write({
        id: request.id,
        error: {
          code: -32000,
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  #write(payload: unknown): void {
    this.#output.write(`${JSON.stringify(payload)}\n`);
  }
}
