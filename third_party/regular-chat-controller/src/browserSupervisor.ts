import { randomUUID } from "node:crypto";
import { mkdir } from "node:fs/promises";
import path from "node:path";

import { chromium, type BrowserContext, type Page } from "playwright";

import { acquireProfileLock, type ProfileLockHandle } from "./profileLock.js";
import type { BrowserEngine } from "./types.js";

export interface BrowserSupervisorOptions {
  runtimeRoot: string;
  profileId?: string;
  engine?: BrowserEngine;
  headed?: boolean;
}

export interface BrowserSnapshot {
  instanceId: string | null;
  profileId: string;
  engine: BrowserEngine;
  connected: boolean;
  pageCount: number;
}

export class BrowserSupervisor {
  private context: BrowserContext | null = null;
  private profileLock: ProfileLockHandle | null = null;
  private instanceId: string | null = null;
  private closeCleanup: Promise<void> = Promise.resolve();
  private startPromise: Promise<BrowserSnapshot> | null = null;
  private stopPromise: Promise<void> | null = null;
  private readonly profileId: string;
  private readonly engine: BrowserEngine;
  private readonly headed: boolean;

  constructor(private readonly options: BrowserSupervisorOptions) {
    if (!path.isAbsolute(options.runtimeRoot)) throw new Error("runtimeRoot must be absolute");
    this.profileId = options.profileId ?? "default-managed";
    this.engine = options.engine ?? "managed-chromium";
    this.headed = options.headed ?? true;
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(this.profileId)) throw new Error("invalid profileId");
  }

  get browserInstanceId(): string | null {
    return this.instanceId;
  }

  get persistentContext(): BrowserContext {
    if (!this.context) throw new Error("browser supervisor is not running");
    return this.context;
  }

  private profileDir(): string {
    const engineSuffix = this.engine === "managed-chromium" ? "managed" : this.engine;
    return path.join(this.options.runtimeRoot, "profiles", `${this.profileId}-${engineSuffix}`);
  }

  private lockPath(): string {
    return path.join(this.options.runtimeRoot, "locks", `${this.profileId}-${this.engine}.lock`);
  }

  async start(): Promise<BrowserSnapshot> {
    if (this.stopPromise) {
      await this.stopPromise;
      return this.start();
    }
    if (this.startPromise) return this.startPromise;
    if (this.context) return this.snapshot();

    const starting = this.startOwned();
    this.startPromise = starting;
    try {
      return await starting;
    } finally {
      if (this.startPromise === starting) this.startPromise = null;
    }
  }

  private async startOwned(): Promise<BrowserSnapshot> {
    await this.closeCleanup;
    await mkdir(this.options.runtimeRoot, { recursive: true });
    await mkdir(this.profileDir(), { recursive: true });
    this.profileLock = await acquireProfileLock(this.lockPath(), {
      controllerInstanceId: `regular-chat-${process.pid}-${randomUUID()}`,
    });
    this.instanceId = randomUUID();
    try {
      const launchOptions = {
        headless: !this.headed,
        viewport: null,
        args: ["--disable-background-timer-throttling"],
      };
      if (this.engine === "msedge") {
        this.context = await chromium.launchPersistentContext(this.profileDir(), {
          ...launchOptions,
          channel: "msedge",
        });
      } else if (this.engine === "chrome") {
        this.context = await chromium.launchPersistentContext(this.profileDir(), {
          ...launchOptions,
          channel: "chrome",
        });
      } else {
        this.context = await chromium.launchPersistentContext(this.profileDir(), launchOptions);
      }
      const ownedContext = this.context;
      const ownedLock = this.profileLock;
      ownedContext.on("close", () => {
        if (this.context !== ownedContext) return;
        this.context = null;
        this.instanceId = null;
        if (this.profileLock === ownedLock) this.profileLock = null;
        this.closeCleanup = ownedLock
          ? ownedLock.release().catch(() => undefined)
          : Promise.resolve();
      });
      return this.snapshot();
    } catch (error) {
      this.instanceId = null;
      await this.profileLock.release().catch(() => undefined);
      this.profileLock = null;
      throw error;
    }
  }

  async newPage(): Promise<Page> {
    const context = this.persistentContext;
    return context.newPage();
  }

  pages(): Page[] {
    return this.context?.pages() ?? [];
  }

  snapshot(): BrowserSnapshot {
    return {
      instanceId: this.instanceId,
      profileId: this.profileId,
      engine: this.engine,
      connected: this.context !== null,
      pageCount: this.context?.pages().length ?? 0,
    };
  }

  async stop(): Promise<void> {
    if (this.stopPromise) return this.stopPromise;
    const starting = this.startPromise;
    const stopping = this.stopOwned(starting);
    this.stopPromise = stopping;
    try {
      await stopping;
    } finally {
      if (this.stopPromise === stopping) this.stopPromise = null;
    }
  }

  private async stopOwned(starting: Promise<BrowserSnapshot> | null): Promise<void> {
    if (starting) await starting.catch(() => undefined);
    const context = this.context;
    const lock = this.profileLock;
    this.context = null;
    this.profileLock = null;
    if (context) await context.close().catch(() => undefined);
    this.instanceId = null;
    if (lock) await lock.release().catch(() => undefined);
    await this.closeCleanup;
  }
}
