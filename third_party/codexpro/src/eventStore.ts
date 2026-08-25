import type {
  EventId,
  EventStore,
  StreamId
} from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

interface StoredEvent {
  eventId: EventId;
  streamId: StreamId;
  message: JSONRPCMessage | null;
  bytes: number;
  createdAt: number;
}

export interface EventStoreStats {
  events: number;
  bytes: number;
  maxEvents: number;
  maxBytes: number;
  ttlMs: number;
}

/**
 * Small, per-session MCP event store for Streamable HTTP reconnect/resume.
 *
 * The official transport assigns each stored message an SSE event id. A client
 * that reconnects with Last-Event-ID can then replay later messages. This store
 * deliberately stays in memory: durable workflow truth belongs to long_run_*,
 * while this buffer only bridges transient HTTP/SSE disconnects within a live
 * CodexPro session.
 */
export class BoundedInMemoryEventStore implements EventStore {
  private readonly events: StoredEvent[] = [];
  private nextId = 0;
  private totalBytes = 0;

  constructor(
    private readonly maxEvents = 256,
    private readonly maxBytes = 4 * 1024 * 1024,
    private readonly ttlMs = 30 * 60 * 1000
  ) {
    if (maxEvents < 1) throw new RangeError("maxEvents must be positive");
    if (maxBytes < 1024) throw new RangeError("maxBytes must be at least 1024");
    if (ttlMs < 1_000) throw new RangeError("ttlMs must be at least 1000");
  }

  private prune(now = Date.now()): void {
    while (this.events.length && now - this.events[0].createdAt > this.ttlMs) {
      const removed = this.events.shift();
      if (removed) this.totalBytes -= removed.bytes;
    }
    while (this.events.length > this.maxEvents) {
      const removed = this.events.shift();
      if (removed) this.totalBytes -= removed.bytes;
    }
    while (this.events.length > 1 && this.totalBytes > this.maxBytes) {
      const removed = this.events.shift();
      if (removed) this.totalBytes -= removed.bytes;
    }
  }

  async storeEvent(streamId: StreamId, message: JSONRPCMessage): Promise<EventId> {
    const now = Date.now();
    const bytes = Buffer.byteLength(JSON.stringify(message), "utf8");
    const eventId = `${streamId}:${now.toString(36)}:${(++this.nextId).toString(36)}`;
    if (bytes > this.maxBytes) {
      this.events.push({ eventId, streamId, message: null, bytes: 0, createdAt: now });
      this.prune(now);
      return eventId;
    }
    this.events.push({ eventId, streamId, message, bytes, createdAt: now });
    this.totalBytes += bytes;
    this.prune(now);
    return eventId;
  }

  async replayEventsAfter(
    lastEventId: EventId,
    { send }: { send: (eventId: EventId, message: JSONRPCMessage) => Promise<void> }
  ): Promise<StreamId> {
    this.prune();
    const index = this.events.findIndex((event) => event.eventId === lastEventId);
    if (index < 0) throw new Error(`Event not retained: ${lastEventId}`);
    const streamId = this.events[index].streamId;
    for (const event of this.events.slice(index + 1)) {
      if (event.streamId !== streamId) continue;
      if (event.message === null) {
        throw new Error(
          `Event replay gap: ${event.eventId} exceeded the bounded resumable buffer`
        );
      }
      await send(event.eventId, event.message);
    }
    return streamId;
  }

  stats(): EventStoreStats {
    this.prune();
    return {
      events: this.events.length,
      bytes: this.totalBytes,
      maxEvents: this.maxEvents,
      maxBytes: this.maxBytes,
      ttlMs: this.ttlMs
    };
  }
}
