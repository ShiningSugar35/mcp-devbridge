import { createHash, randomUUID } from "node:crypto";
import path from "node:path";

import type { Page } from "playwright";

import { ChatGPTAdapter } from "./adapters/chatgpt.js";
import type { ProviderAdapter, ProviderObservation } from "./adapters/provider.js";
import { BrowserSupervisor, type BrowserSupervisorOptions } from "./browserSupervisor.js";
import { CompletionDetector } from "./completionDetector.js";
import { MIN_POLL_INTERVAL_MS, MAX_POLL_INTERVAL_MS } from "./limits.js";
import { createTurnIdentity } from "./promptHash.js";
import { RecoveryCoordinator } from "./recoveryCoordinator.js";
import { isSendConfirmationSafe } from "./sendConfirmation.js";
import { ProviderSessionStore, summarizeSession } from "./sessionStore.js";
import { decidePersistedTurnPreflight, transitionSendState } from "./sendStateMachine.js";
import { TabRegistry } from "./tabRegistry.js";
import type { IntentClass, PersistedTurnState, ProviderSessionState, TabLease } from "./types.js";

interface RunContext {
  workspaceHash: string;
  runId: string;
  page: Page;
  pageId: string;
  assistantTurnsBefore: number;
}

export interface ControllerOptions extends BrowserSupervisorOptions {
  pollIntervalMs?: number;
  stableOutputWindowMs?: number;
  adapter?: ProviderAdapter;
}

export interface OpenRunInput {
  workspaceHash: string;
  runId: string;
  conversationUrl?: string;
}

export interface SendInput {
  runId: string;
  prompt: string;
  localTurnId?: string;
  intentClass?: IntentClass;
}

export interface WatchInput {
  runId: string;
  timeoutMs?: number;
}

interface RecoveryObservationResult {
  observation: ProviderObservation | null;
  userReplyRequired: boolean;
  reason: string;
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function conversationRefFromUrl(url: string): string | null {
  return /^https:\/\/chatgpt\.com\/c\/([A-Za-z0-9_-]+)/.exec(url)?.[1] ?? null;
}

function boundedPoll(value: number | undefined): number {
  const poll = value ?? 750;
  if (!Number.isInteger(poll) || poll < MIN_POLL_INTERVAL_MS || poll > MAX_POLL_INTERVAL_MS) {
    throw new Error(`pollIntervalMs must be ${MIN_POLL_INTERVAL_MS}..${MAX_POLL_INTERVAL_MS}`);
  }
  return poll;
}

export class RegularChatController {
  private readonly browser: BrowserSupervisor;
  private readonly adapter: ProviderAdapter;
  private readonly tabs = new TabRegistry();
  private readonly recovery = new RecoveryCoordinator();
  private readonly sessions: ProviderSessionStore;
  private readonly runs = new Map<string, RunContext>();
  private readonly recoveries = new Map<string, Promise<RecoveryObservationResult>>();
  private readonly inFlightSends = new Map<string, { requestKey: string | null; promise: Promise<object> }>();
  private readonly pollIntervalMs: number;
  private readonly stableOutputWindowMs: number | undefined;

  constructor(private readonly options: ControllerOptions) {
    this.browser = new BrowserSupervisor(options);
    this.adapter = options.adapter ?? new ChatGPTAdapter();
    this.sessions = new ProviderSessionStore(path.join(options.runtimeRoot, "sessions"));
    this.pollIntervalMs = boundedPoll(options.pollIntervalMs);
    this.stableOutputWindowMs = options.stableOutputWindowMs;
  }

  async start(): Promise<object> {
    return this.browser.start();
  }

  async login(): Promise<object> {
    await this.browser.start();
    const existing = this.browser.pages().find((page) => page.url().startsWith("https://chatgpt.com"));
    const page = existing ?? await this.browser.newPage();
    if (!page.url().startsWith("https://chatgpt.com")) await this.adapter.openHome(page);
    const observation = await this.observePage(page);
    return {
      ok: true,
      browser: this.browser.snapshot(),
      conversationUrl: observation.conversationUrl,
      authenticatedUi: observation.loginGate ? "no" : observation.composerReady ? "yes" : "unknown",
      loginGate: observation.loginGate,
      securityGate: observation.securityGate,
      policyGate: observation.policyGate,
      selectorState: observation.selectorState,
    };
  }

  async stop(): Promise<void> {
    for (const context of this.runs.values()) {
      await context.page.close().catch(() => undefined);
    }
    this.runs.clear();
    for (const lease of this.tabs.list()) this.tabs.close(lease.runId);
    await this.browser.stop();
  }

  status(): object {
    return {
      controller: "ready",
      browser: this.browser.snapshot(),
      activeRuns: this.tabs.list().map((lease) => ({
        runId: lease.runId,
        workspaceId: lease.workspaceId,
        state: lease.state,
        conversationRefHash: lease.conversationRef ? sha256(lease.conversationRef).slice(0, 16) : null,
      })),
    };
  }

  async openRun(input: OpenRunInput): Promise<object> {
    this.validateRunIdentity(input.workspaceHash, input.runId);
    await this.browser.start();
    const stored = await this.sessions.load(input.workspaceHash, input.runId);
    const existing = this.runs.get(input.runId);
    if (existing) {
      if (existing.page.isClosed() || !this.browser.snapshot().connected) {
        if (!stored) throw new Error("run page was lost before provider session state existed");
        const recovered = await this.recoverContext(existing, stored);
        if (!recovered.observation) {
          return { ok: false, userReplyRequired: true, reason: recovered.reason };
        }
        return this.describeRun(existing, recovered.observation, summarizeSession(stored));
      }
      return this.describeRun(existing, await this.observePage(existing.page), stored ? summarizeSession(stored) : null);
    }

    if (
      stored &&
      stored.conversation_ref.startsWith("pending:") &&
      stored.current_turn.send_state !== "never_sent"
    ) {
      return { ok: false, userReplyRequired: true, reason: "conversation_identity_unavailable" };
    }
    if (input.conversationUrl && stored && input.conversationUrl !== stored.conversation_url) {
      throw new Error("explicit conversation URL conflicts with durable provider session");
    }

    const page = await this.browser.newPage();
    let leaseAdded = false;
    let runAdded = false;
    try {
      const pageId = randomUUID();
      const targetUrl = stored && !stored.conversation_ref.startsWith("pending:")
        ? stored.conversation_url
        : input.conversationUrl;
      if (targetUrl) await this.adapter.openConversation(page, targetUrl);
      else await this.adapter.openHome(page);
      const observed = await this.observePage(page);
      if (stored && !stored.conversation_ref.startsWith("pending:") && observed.conversationRef !== stored.conversation_ref) {
        throw new Error("conversation identity mismatch during session recovery");
      }
      const lease: TabLease = {
        sessionId: `${input.workspaceHash.slice(0, 16)}:${input.runId}`,
        runId: input.runId,
        workspaceId: input.workspaceHash,
        profileId: this.browser.snapshot().profileId,
        pageId,
        conversationRef: stored && !stored.conversation_ref.startsWith("pending:")
          ? stored.conversation_ref
          : observed.conversationRef,
        state: stored?.current_turn.response_state === "complete" ? "ready" : stored ? "recovering" : "ready",
      };
      this.tabs.add(lease);
      leaseAdded = true;
      const context: RunContext = {
        workspaceHash: input.workspaceHash,
        runId: input.runId,
        page,
        pageId,
        assistantTurnsBefore: stored?.current_turn.assistant_turn_count_before ?? observed.assistantTurnCount,
      };
      this.runs.set(input.runId, context);
      runAdded = true;
      this.attachPageClose(context, page);
      return this.describeRun(context, observed, stored ? summarizeSession(stored) : null);
    } catch (error) {
      if (runAdded) this.runs.delete(input.runId);
      if (leaseAdded) this.tabs.close(input.runId);
      await page.close().catch(() => undefined);
      throw error;
    }

  }

  async send(input: SendInput): Promise<object> {
    const requestKey = input.localTurnId
      ? `${input.localTurnId}:${createTurnIdentity(input.localTurnId, input.prompt).promptSha256}:${input.intentClass ?? "read_only"}`
      : null;
    const inFlight = this.inFlightSends.get(input.runId);
    if (inFlight) {
      if (requestKey !== null && inFlight.requestKey === requestKey) {
        const result = await inFlight.promise;
        return {
          ...(result as Record<string, unknown>),
          duplicateSuppressed: true,
          reason: "coalesced_in_flight_send",
        };
      }
      return { ok: false, userReplyRequired: true, reason: "send_already_in_progress" };
    }

    const operation = this.sendOwned(input);
    this.inFlightSends.set(input.runId, { requestKey, promise: operation });
    try {
      return await operation;
    } finally {
      if (this.inFlightSends.get(input.runId)?.promise === operation) {
        this.inFlightSends.delete(input.runId);
      }
    }
  }

  private async sendOwned(input: SendInput): Promise<object> {
    const context = this.requireRun(input.runId);
    const intentClass = input.intentClass ?? "read_only";
    const incomingLocalTurnId = input.localTurnId ?? null;
    const incomingPromptSha256 = createTurnIdentity(incomingLocalTurnId ?? "preflight", input.prompt).promptSha256;
    const stored = await this.sessions.load(context.workspaceHash, context.runId);
    const preflight = decidePersistedTurnPreflight({
      persistedTurn: stored?.current_turn ?? null,
      incomingLocalTurnId,
      incomingPromptSha256,
      incomingIntentClass: intentClass,
    });
    if (preflight.action === "pause_ambiguous") {
      this.tabs.setState(input.runId, "recovering");
      return {
        ok: false,
        userReplyRequired: true,
        reason: preflight.reason,
        localTurnId: stored?.current_turn.local_turn_id ?? incomingLocalTurnId,
      };
    }
    if (preflight.action === "observe_existing") {
      return {
        ok: true,
        userReplyRequired: false,
        duplicateSuppressed: true,
        watchRequired: true,
        reason: preflight.reason,
        localTurnId: stored?.current_turn.local_turn_id,
        promptSha256: stored?.current_turn.prompt_sha256,
      };
    }
    if (preflight.action === "already_complete") {
      return {
        ok: true,
        complete: true,
        userReplyRequired: false,
        duplicateSuppressed: true,
        reason: preflight.reason,
        localTurnId: stored?.current_turn.local_turn_id,
        promptSha256: stored?.current_turn.prompt_sha256,
        responseSha256: stored?.current_turn.response_sha256,
      };
    }

    const observedBefore = stored
      ? await this.observeWithRecovery(context, stored)
      : {
          observation: await this.observePage(context.page),
          userReplyRequired: false,
          reason: "page_observed",
        };
    if (!observedBefore.observation) {
      return {
        ok: false,
        userReplyRequired: observedBefore.userReplyRequired,
        reason: observedBefore.reason,
      };
    }
    const before = observedBefore.observation;
    if (before.loginGate || before.securityGate || before.policyGate) {
      return { ok: false, userReplyRequired: true, reason: "login_or_security_gate" };
    }
    if (!before.composerReady) throw new Error("composer is not ready");
    if (this.tabs.get(input.runId)?.conversationRef && before.conversationRef) {
      this.tabs.assertConversation(input.runId, before.conversationRef);
    }

    const localTurnId = preflight.action === "resume_never_sent"
      ? stored!.current_turn.local_turn_id
      : input.localTurnId ?? randomUUID();
    const identity = createTurnIdentity(localTurnId, input.prompt);
    let turn: PersistedTurnState = preflight.action === "resume_never_sent"
      ? { ...stored!.current_turn }
      : {
          local_turn_id: identity.localTurnId,
          prompt_sha256: identity.promptSha256,
          intent_class: intentClass,
          assistant_turn_count_before: before.assistantTurnCount,
          send_state: "never_sent",
          response_state: "waiting_for_turn",
          response_sha256: null,
        };
    await this.sessions.save(this.sessionState(context, before, turn));
    const previousTabState = this.tabs.get(input.runId)?.state ?? "ready";
    this.tabs.setState(input.runId, "generating");
    try {
      turn = { ...turn, send_state: transitionSendState(turn.send_state, "send_started") };
      await this.sessions.save(this.sessionState(context, before, turn));
    } catch (error) {
      this.tabs.setState(input.runId, previousTabState);
      throw error;
    }
    context.assistantTurnsBefore = before.assistantTurnCount;
    try {
      await this.adapter.sendPrompt(context.page, input.prompt);
    } catch (error) {
      turn = { ...turn, send_state: "ambiguous", response_state: "ambiguous" };
      await this.sessions.save(this.sessionState(context, before, turn));
      this.tabs.setState(input.runId, "recovering");
      throw error;
    }

    const confirmation = await this.waitForSendConfirmation(context, before, 8_000);
    if (!confirmation.confirmed) {
      turn = { ...turn, send_state: "ambiguous", response_state: "ambiguous" };
      await this.sessions.save(this.sessionState(context, confirmation.observation, turn));
      this.tabs.setState(input.runId, "recovering");
      return { ok: false, userReplyRequired: true, reason: "send_confirmation_ambiguous" };
    }
    turn = { ...turn, send_state: "send_confirmed", response_state: "generating" };
    if (confirmation.observation.conversationRef) {
      this.tabs.bindConversation(input.runId, confirmation.observation.conversationRef);
    }
    await this.sessions.save(this.sessionState(context, confirmation.observation, turn));
    return {
      ok: true,
      localTurnId,
      promptSha256: identity.promptSha256,
      conversationRefHash: confirmation.observation.conversationRef
        ? sha256(confirmation.observation.conversationRef).slice(0, 16)
        : null,
    };
  }

  async watch(input: WatchInput): Promise<object> {
    const context = this.requireRun(input.runId);
    const timeoutMs = Math.max(1_000, Math.min(input.timeoutMs ?? 120_000, 600_000));
    const stored = await this.sessions.load(context.workspaceHash, context.runId);
    if (!stored) throw new Error("provider session does not exist; send a turn first");
    const detector = new CompletionDetector(
      this.stableOutputWindowMs === undefined ? {} : { stableWindowMs: this.stableOutputWindowMs },
    );
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const observed = await this.observeWithRecovery(context, stored);
      if (!observed.observation) {
        if (observed.userReplyRequired) {
          this.tabs.setState(input.runId, "recovering");
          return { ok: false, complete: false, userReplyRequired: true, reason: observed.reason };
        }
        detector.reset();
        await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
        continue;
      }
      const observation = observed.observation;
      const expectedRef = stored.conversation_ref.startsWith("pending:") ? observation.conversationRef : stored.conversation_ref;
      const decision = this.recovery.decide({
        expectedConversationRef: expectedRef,
        observedConversationRef: observation.conversationRef,
        intentClass: stored.current_turn.intent_class,
        sendState: stored.current_turn.send_state,
        pageTurnEvidence: observation.assistantTurnCount > context.assistantTurnsBefore ? "present" : "unknown",
        responseState: stored.current_turn.response_state,
        tabState: context.page.isClosed() ? "closed" : "open",
        browserState: this.browser.snapshot().connected ? "connected" : "disconnected",
        networkState: observation.networkState,
        selectorState: observation.selectorState,
        loginGate: observation.loginGate,
        securityGate: observation.securityGate,
        policyGate: observation.policyGate,
        userReplyRequired: false,
        nextPromptExists: false,
      });
      if (decision.userReplyRequired) {
        this.tabs.setState(input.runId, "recovering");
        return { ok: false, complete: false, userReplyRequired: true, reason: decision.reason };
      }
      if (decision.action === "wait_for_network") {
        detector.reset();
        await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
        continue;
      }
      if (decision.action === "recover_tab" || decision.action === "restart_browser" || decision.action === "reload_conversation") {
        detector.reset();
        const recovered = await this.recoverContext(context, stored);
        if (recovered.userReplyRequired) {
          return { ok: false, complete: false, userReplyRequired: true, reason: recovered.reason };
        }
        await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
        continue;
      }
      const result = detector.evaluate({
        nowMs: Date.now(),
        assistantTurnCountBefore: context.assistantTurnsBefore,
        assistantTurnCountAfter: observation.assistantTurnCount,
        assistantText: observation.assistantText,
        assistantTextHash: sha256(observation.assistantText),
        generationControlPresent: observation.generationControlPresent,
        composerReady: observation.composerReady,
        finalControlsPresent: observation.finalControlsPresent,
        selectorState: observation.selectorState,
        networkState: observation.networkState,
      });
      if (result.complete) {
        const markdown = await this.adapter.extractMarkdown(context.page);
        const updated: ProviderSessionState = {
          ...stored,
          browser_instance_id: this.browser.browserInstanceId ?? stored.browser_instance_id,
          page_id: context.pageId,
          conversation_ref: observation.conversationRef ?? stored.conversation_ref,
          conversation_url: observation.conversationUrl,
          current_turn: {
            ...stored.current_turn,
            response_state: "complete",
            response_sha256: sha256(markdown),
          },
          updated_at: new Date().toISOString(),
        };
        await this.sessions.save(updated);
        this.tabs.setState(input.runId, "ready");
        return {
          ok: true,
          complete: true,
          userReplyRequired: false,
          markdown,
          responseSha256: updated.current_turn.response_sha256,
          selectorFallbackUsed: result.selectorFallbackUsed,
        };
      }
      await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
    }
    return { ok: true, complete: false, userReplyRequired: false, reason: "watch_timeout" };
  }

  async continueTurn(input: SendInput): Promise<object> {
    const context = this.requireRun(input.runId);
    const stored = await this.sessions.load(context.workspaceHash, context.runId);
    if (!stored) throw new Error("provider session does not exist; no completed turn to continue from");
    const observed = await this.observeWithRecovery(context, stored);
    if (!observed.observation) {
      return {
        ok: false,
        continued: false,
        userReplyRequired: observed.userReplyRequired,
        reason: observed.reason,
      };
    }
    const observation = observed.observation;
    const expectedRef = stored.conversation_ref.startsWith("pending:")
      ? observation.conversationRef
      : stored.conversation_ref;
    const decision = this.recovery.decide({
      expectedConversationRef: expectedRef,
      observedConversationRef: observation.conversationRef,
      intentClass: stored.current_turn.intent_class,
      sendState: stored.current_turn.send_state,
      pageTurnEvidence: observation.assistantTurnCount > context.assistantTurnsBefore ? "present" : "unknown",
      responseState: stored.current_turn.response_state,
      tabState: context.page.isClosed() ? "closed" : "open",
      browserState: this.browser.snapshot().connected ? "connected" : "disconnected",
      networkState: observation.networkState,
      selectorState: observation.selectorState,
      loginGate: observation.loginGate,
      securityGate: observation.securityGate,
      policyGate: observation.policyGate,
      userReplyRequired: false,
      nextPromptExists: input.prompt.length > 0,
    });
    if (!decision.safeToAutoContinue) {
      return {
        ok: false,
        continued: false,
        userReplyRequired: decision.userReplyRequired,
        reason: decision.reason,
      };
    }
    const result = await this.send(input);
    return { ...result, continued: true };
  }

  async resume(input: OpenRunInput): Promise<object> {
    return this.openRun(input);
  }

  async closeRun(runId: string): Promise<object> {
    const context = this.runs.get(runId);
    if (!context) return { ok: true, closed: false };
    this.runs.delete(runId);
    this.tabs.close(runId);
    await context.page.close().catch(() => undefined);
    return { ok: true, closed: true };
  }

  private async observePage(page: Page): Promise<ProviderObservation> {
    const observation = await this.adapter.observe(page);
    const urlConversationRef = conversationRefFromUrl(observation.conversationUrl);
    if (
      observation.conversationRef !== null &&
      urlConversationRef !== null &&
      observation.conversationRef !== urlConversationRef
    ) {
      throw new Error("provider observation conversation identity mismatch");
    }
    return {
      ...observation,
      conversationRef: observation.conversationRef ?? urlConversationRef,
    };
  }

  private attachPageClose(context: RunContext, page: Page): void {
    page.once("close", () => {
      if (this.runs.get(context.runId) !== context || context.page !== page) return;
      const lease = this.tabs.get(context.runId);
      if (lease) this.tabs.setState(context.runId, "recovering");
    });
  }

  private async observeWithRecovery(
    context: RunContext,
    stored: ProviderSessionState,
  ): Promise<RecoveryObservationResult> {
    if (context.page.isClosed() || !this.browser.snapshot().connected) {
      return this.recoverContext(context, stored);
    }
    try {
      return {
        observation: await this.observePage(context.page),
        userReplyRequired: false,
        reason: "page_observed",
      };
    } catch (error) {
      if (context.page.isClosed() || !this.browser.snapshot().connected) {
        return this.recoverContext(context, stored);
      }
      throw error;
    }
  }

  private async recoverContext(
    context: RunContext,
    stored: ProviderSessionState,
  ): Promise<RecoveryObservationResult> {
    const existing = this.recoveries.get(context.runId);
    if (existing) return existing;
    const recovery = this.doRecoverContext(context, stored);
    this.recoveries.set(context.runId, recovery);
    try {
      return await recovery;
    } finally {
      if (this.recoveries.get(context.runId) === recovery) this.recoveries.delete(context.runId);
    }
  }

  private async doRecoverContext(
    context: RunContext,
    stored: ProviderSessionState,
  ): Promise<RecoveryObservationResult> {
    if (stored.conversation_ref.startsWith("pending:")) {
      return {
        observation: null,
        userReplyRequired: true,
        reason: "conversation_identity_unavailable",
      };
    }
    try {
      await this.browser.start();
      const page = await this.browser.newPage();
      const pageId = randomUUID();
      try {
        await this.adapter.openConversation(page, stored.conversation_url);
        const observation = await this.observePage(page);
        if (observation.loginGate || observation.securityGate || observation.policyGate) {
          await page.close().catch(() => undefined);
          return { observation: null, userReplyRequired: true, reason: "user_or_security_gate" };
        }
        if (observation.conversationRef !== stored.conversation_ref) {
          await page.close().catch(() => undefined);
          return { observation: null, userReplyRequired: true, reason: "conversation_identity_mismatch" };
        }

        const oldPage = context.page;
        context.page = page;
        context.pageId = pageId;
        context.assistantTurnsBefore = stored.current_turn.assistant_turn_count_before;
        const leaseState = stored.current_turn.response_state === "complete" ? "ready" : "generating";
        this.tabs.replacePage(context.runId, pageId, leaseState);
        this.tabs.bindConversation(context.runId, stored.conversation_ref);
        this.attachPageClose(context, page);
        await this.sessions.save({
          ...stored,
          browser_instance_id: this.browser.browserInstanceId ?? stored.browser_instance_id,
          page_id: pageId,
          conversation_url: observation.conversationUrl,
          updated_at: new Date().toISOString(),
        });
        if (!oldPage.isClosed()) await oldPage.close().catch(() => undefined);
        return { observation, userReplyRequired: false, reason: "conversation_recovered" };
      } catch (error) {
        await page.close().catch(() => undefined);
        throw error;
      }
    } catch {
      return {
        observation: null,
        userReplyRequired: false,
        reason: "conversation_reload_retryable",
      };
    }
  }

  private async waitForSendConfirmation(
    context: RunContext,
    before: ProviderObservation,
    timeoutMs: number,
  ): Promise<{ confirmed: boolean; observation: ProviderObservation }> {
    const deadline = Date.now() + timeoutMs;
    let last = before;
    while (Date.now() < deadline) {
      last = await this.observePage(context.page);
      const generationStarted = last.generationControlPresent;
      const assistantTurnStarted = last.assistantTurnCount > before.assistantTurnCount;
      if (isSendConfirmationSafe({
        beforeConversationRef: before.conversationRef,
        afterConversationRef: last.conversationRef,
        generationStarted,
        assistantTurnStarted,
      })) {
        return { confirmed: true, observation: last };
      }
      await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
    }
    return { confirmed: false, observation: last };
  }

  private sessionState(
    context: RunContext,
    observation: ProviderObservation,
    turn: PersistedTurnState,
  ): ProviderSessionState {
    return {
      schema_version: 2,
      workspace_hash: context.workspaceHash,
      durable_run_id: context.runId,
      profile_id: this.browser.snapshot().profileId,
      browser_engine: this.browser.snapshot().engine,
      browser_instance_id: this.browser.browserInstanceId ?? "browser-unavailable",
      page_id: context.pageId,
      conversation_ref: observation.conversationRef ?? `pending:${context.runId}`,
      conversation_url: observation.conversationRef ? observation.conversationUrl : "https://chatgpt.com/",
      current_turn: turn,
      updated_at: new Date().toISOString(),
    };
  }

  private describeRun(context: RunContext, observation: ProviderObservation, session: object | null = null): object {
    return {
      ok: true,
      runId: context.runId,
      pageId: context.pageId,
      conversationRefHash: observation.conversationRef ? sha256(observation.conversationRef).slice(0, 16) : null,
      conversationUrl: observation.conversationUrl,
      loginGate: observation.loginGate,
      securityGate: observation.securityGate,
      policyGate: observation.policyGate,
      selectorState: observation.selectorState,
      session,
    };
  }

  private requireRun(runId: string): RunContext {
    const context = this.runs.get(runId);
    if (!context) throw new Error(`run is not open: ${runId}`);
    return context;
  }

  private validateRunIdentity(workspaceHash: string, runId: string): void {
    if (!/^[a-f0-9]{64}$/.test(workspaceHash)) throw new Error("workspaceHash must be SHA-256");
    if (!/^[A-Za-z0-9._-]{1,160}$/.test(runId)) throw new Error("invalid runId");
  }
}
