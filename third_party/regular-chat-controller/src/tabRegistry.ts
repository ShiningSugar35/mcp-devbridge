import { MAX_ACTIVE_CHAT_TABS, MAX_GENERATING_CHAT_TABS } from "./limits.js";
import type { TabLease, TabLeaseState } from "./types.js";

export interface TabRegistryOptions {
  maxActiveTabs?: number;
  maxGeneratingTabs?: number;
}

export class TabRegistry {
  private readonly leasesByRun = new Map<string, TabLease>();
  private readonly runByPage = new Map<string, string>();
  private readonly maxActiveTabs: number;
  private readonly maxGeneratingTabs: number;

  constructor(options: TabRegistryOptions = {}) {
    this.maxActiveTabs = options.maxActiveTabs ?? MAX_ACTIVE_CHAT_TABS;
    this.maxGeneratingTabs = options.maxGeneratingTabs ?? MAX_GENERATING_CHAT_TABS;
    if (this.maxActiveTabs < 1 || this.maxActiveTabs > MAX_ACTIVE_CHAT_TABS) {
      throw new Error("invalid active tab limit");
    }
    if (this.maxGeneratingTabs < 1 || this.maxGeneratingTabs > this.maxActiveTabs) {
      throw new Error("invalid generating tab limit");
    }
  }

  get size(): number {
    return this.leasesByRun.size;
  }

  add(lease: TabLease): TabLease {
    if (this.leasesByRun.has(lease.runId)) {
      throw new Error(`run already has a tab lease: ${lease.runId}`);
    }
    const owner = this.runByPage.get(lease.pageId);
    if (owner) {
      throw new Error(`page already owned by run ${owner}`);
    }
    if (this.leasesByRun.size >= this.maxActiveTabs) {
      throw new Error(`active tab limit exceeded (${this.maxActiveTabs})`);
    }
    if (lease.state === "generating" && this.generatingCount() >= this.maxGeneratingTabs) {
      throw new Error(`generating tab limit exceeded (${this.maxGeneratingTabs})`);
    }
    const stored = { ...lease };
    this.leasesByRun.set(lease.runId, stored);
    this.runByPage.set(lease.pageId, lease.runId);
    return { ...stored };
  }

  get(runId: string): TabLease | undefined {
    const lease = this.leasesByRun.get(runId);
    return lease ? { ...lease } : undefined;
  }

  assertConversation(runId: string, observedConversationRef: string | null): void {
    const lease = this.leasesByRun.get(runId);
    if (!lease) {
      throw new Error(`run has no tab lease: ${runId}`);
    }
    if (lease.conversationRef !== observedConversationRef) {
      throw new Error(
        `conversation identity mismatch for ${runId}: expected ${lease.conversationRef ?? "null"}`,
      );
    }
  }

  bindConversation(runId: string, conversationRef: string): TabLease {
    if (!conversationRef) throw new Error("conversation ref is required");
    const lease = this.leasesByRun.get(runId);
    if (!lease) throw new Error(`run has no tab lease: ${runId}`);
    if (lease.conversationRef && lease.conversationRef !== conversationRef) {
      throw new Error(`conversation identity mismatch for ${runId}: expected ${lease.conversationRef}`);
    }
    lease.conversationRef = conversationRef;
    return { ...lease };
  }

  setState(runId: string, state: TabLeaseState): TabLease {
    const lease = this.leasesByRun.get(runId);
    if (!lease) {
      throw new Error(`run has no tab lease: ${runId}`);
    }
    if (state === "generating" && lease.state !== "generating") {
      if (this.generatingCount() >= this.maxGeneratingTabs) {
        throw new Error(`generating tab limit exceeded (${this.maxGeneratingTabs})`);
      }
    }
    lease.state = state;
    return { ...lease };
  }

  replacePage(runId: string, pageId: string, state: TabLeaseState = "recovering"): TabLease {
    if (!pageId) throw new Error("page id is required");
    const lease = this.leasesByRun.get(runId);
    if (!lease) throw new Error(`run has no tab lease: ${runId}`);
    const owner = this.runByPage.get(pageId);
    if (owner && owner !== runId) throw new Error(`page already owned by run ${owner}`);
    if (state === "generating" && lease.state !== "generating" && this.generatingCount() >= this.maxGeneratingTabs) {
      throw new Error(`generating tab limit exceeded (${this.maxGeneratingTabs})`);
    }
    this.runByPage.delete(lease.pageId);
    lease.pageId = pageId;
    lease.state = state;
    this.runByPage.set(pageId, runId);
    return { ...lease };
  }

  close(runId: string): TabLease | undefined {
    const lease = this.leasesByRun.get(runId);
    if (!lease) {
      return undefined;
    }
    this.leasesByRun.delete(runId);
    this.runByPage.delete(lease.pageId);
    return { ...lease, state: "closed" };
  }

  list(): TabLease[] {
    return Array.from(this.leasesByRun.values(), (lease) => ({ ...lease }));
  }

  private generatingCount(): number {
    let count = 0;
    for (const lease of this.leasesByRun.values()) {
      if (lease.state === "generating") {
        count += 1;
      }
    }
    return count;
  }
}
