import { DEFAULT_STABLE_OUTPUT_WINDOW_MS } from "./limits.js";
import type { NetworkState, ResponseState, SelectorState } from "./types.js";

export interface CompletionSnapshot {
  nowMs: number;
  assistantTurnCountBefore: number;
  assistantTurnCountAfter: number;
  assistantText: string;
  assistantTextHash: string;
  generationControlPresent: boolean;
  composerReady: boolean;
  finalControlsPresent: boolean;
  selectorState: SelectorState;
  networkState: NetworkState;
}

export interface CompletionResult {
  state: ResponseState;
  complete: boolean;
  stableForMs: number;
  selectorFallbackUsed: boolean;
  reason: string;
}

export interface CompletionDetectorOptions {
  stableWindowMs?: number;
}

export class CompletionDetector {
  private readonly stableWindowMs: number;
  private candidateHash: string | null = null;
  private candidateSinceMs: number | null = null;

  constructor(options: CompletionDetectorOptions = {}) {
    const requested = options.stableWindowMs ?? DEFAULT_STABLE_OUTPUT_WINDOW_MS;
    if (!Number.isFinite(requested) || requested < 0 || requested > 30_000) {
      throw new Error("stableWindowMs must be between 0 and 30000");
    }
    this.stableWindowMs = requested;
  }

  evaluate(snapshot: CompletionSnapshot): CompletionResult {
    const selectorFallbackUsed = snapshot.selectorState === "fallback";
    if (snapshot.selectorState === "missing") {
      this.resetCandidate();
      return this.result("page_broken", false, 0, selectorFallbackUsed, "required_selectors_missing");
    }
    if (snapshot.networkState !== "online") {
      this.resetCandidate();
      return this.result("ambiguous", false, 0, selectorFallbackUsed, "network_not_confirmed_online");
    }
    if (snapshot.assistantTurnCountAfter <= snapshot.assistantTurnCountBefore) {
      this.resetCandidate();
      return this.result("waiting_for_turn", false, 0, selectorFallbackUsed, "new_assistant_turn_not_observed");
    }
    if (snapshot.generationControlPresent) {
      this.resetCandidate();
      return this.result("generating", false, 0, selectorFallbackUsed, "generation_control_present");
    }

    const generationEndEvidence = snapshot.composerReady || snapshot.finalControlsPresent;
    if (!generationEndEvidence) {
      this.resetCandidate();
      return this.result("ambiguous", false, 0, selectorFallbackUsed, "generation_end_not_proven");
    }

    if (this.candidateHash !== snapshot.assistantTextHash || this.candidateSinceMs === null) {
      this.candidateHash = snapshot.assistantTextHash;
      this.candidateSinceMs = snapshot.nowMs;
      return this.result("candidate_complete", false, 0, selectorFallbackUsed, "stable_window_started");
    }

    const stableForMs = Math.max(0, snapshot.nowMs - this.candidateSinceMs);
    if (stableForMs < this.stableWindowMs) {
      return this.result(
        "candidate_complete",
        false,
        stableForMs,
        selectorFallbackUsed,
        "stable_window_pending",
      );
    }

    const finalConfidence = snapshot.finalControlsPresent || snapshot.composerReady;
    if (!finalConfidence) {
      return this.result("candidate_complete", false, stableForMs, selectorFallbackUsed, "final_signal_missing");
    }
    return this.result("complete", true, stableForMs, selectorFallbackUsed, "completion_confirmed");
  }

  reset(): void {
    this.resetCandidate();
  }

  private resetCandidate(): void {
    this.candidateHash = null;
    this.candidateSinceMs = null;
  }

  private result(
    state: ResponseState,
    complete: boolean,
    stableForMs: number,
    selectorFallbackUsed: boolean,
    reason: string,
  ): CompletionResult {
    return { state, complete, stableForMs, selectorFallbackUsed, reason };
  }
}
