export interface GracefulHttpServer {
  close(callback: (error?: Error) => void): unknown;
  closeAllConnections?: () => void;
}

export interface GracefulShutdownOptions {
  server: GracefulHttpServer;
  cleanup?: () => Promise<void> | void;
  timeoutMs?: number;
  logger?: (message: string) => void;
  exit?: (code: number) => void;
}

export interface GracefulShutdownController {
  shutdown(signal: string): Promise<void>;
}

export function createGracefulShutdownController(
  options: GracefulShutdownOptions
): GracefulShutdownController {
  const timeoutMs = Math.max(1, options.timeoutMs ?? 5_000);
  const cleanup = options.cleanup ?? (() => undefined);
  const logger = options.logger ?? (() => undefined);
  const exit = options.exit ?? ((code: number) => {
    process.exitCode = code;
  });
  let shutdownPromise: Promise<void> | null = null;

  const settleWithin = (promise: Promise<void>, waitMs: number): Promise<boolean> =>
    new Promise<boolean>((resolve, reject) => {
      const timer = setTimeout(() => resolve(false), waitMs);
      promise.then(
        () => {
          clearTimeout(timer);
          resolve(true);
        },
        (error) => {
          clearTimeout(timer);
          reject(error);
        }
      );
    });

  const shutdown = (signal: string): Promise<void> => {
    if (shutdownPromise) return shutdownPromise;

    shutdownPromise = (async () => {
      logger(`graceful shutdown start: ${signal}`);

      const closePromise = new Promise<void>((resolve, reject) => {
        try {
          options.server.close((error?: Error) => {
            if (error) reject(error);
            else resolve();
          });
        } catch (error) {
          reject(error);
        }
      });

      try {
        const drained = await settleWithin(closePromise, timeoutMs);
        if (!drained) {
          if (typeof options.server.closeAllConnections !== "function") {
            throw new Error(
              `graceful shutdown drain exceeded ${timeoutMs}ms and the server cannot force-close lingering connections`
            );
          }
          options.server.closeAllConnections();
          logger(`graceful shutdown drain timeout after ${timeoutMs}ms; forced lingering connections closed`);
        }

        // Dispose MCP runtime/session state only after active HTTP/SSE work has
        // drained, or after the bounded force-close path owns the termination.
        const cleanupPromise = Promise.resolve().then(cleanup);
        const cleaned = await settleWithin(cleanupPromise, timeoutMs);
        if (!cleaned) {
          logger(`graceful shutdown cleanup timeout after ${timeoutMs}ms`);
          exit(1);
          return;
        }
        logger("graceful shutdown complete");
        exit(0);
      } catch (error) {
        logger(`graceful shutdown failed: ${error instanceof Error ? error.message : String(error)}`);
        try {
          options.server.closeAllConnections?.();
        } finally {
          exit(1);
        }
      }
    })();

    return shutdownPromise;
  };

  return { shutdown };
}
