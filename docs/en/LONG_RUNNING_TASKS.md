# Long-running task orchestration (v0.8.7)

## Problem

The durable orchestration layer was introduced in v0.8.4. v0.8.5 hardened the shared Hub/Tunnel and Gateway stream lifecycle; v0.8.6 added Goal-like same-turn autonomy hints, application-layer SSE liveness, capability-sensitive MCP progress, bounded event replay, and compact running-poll payloads. v0.8.7 moves CodexPro HTTP to MCP SDK v2 stateless/hybrid serving and separates transport state from process-level business state.

Browser-hosted MCP clients are a poor place to keep a completely silent tool request open for minutes or hours, but returning to the user every few minutes is also a poor Goal-mode experience. v0.8.7 therefore separates **execution lifetime** from **MCP request lifetime** while keeping the active assistant turn busy and observable for as long as the host permits. If the ChatGPT host itself ends the turn or times out message delivery, the durable run remains the recovery source of truth.

A long run is not considered complete merely because an executor command exits. The required lifecycle is:

```text
requirement decomposition
        ↓
durable plan + acceptance criteria
        ↓
execute / checkpoint evidence
        ↓
quality review against current work revision
        ↓
FAIL ──→ explicit rework ──→ review again
        ↓ PASS
completion gate
        ↓
return final completion claim
```

## Research basis

The design follows three converging patterns from current agent/MCP systems:

1. **MCP Tasks**: the Model Context Protocol Tasks extension is designed for long-running operations. A server can return a durable task handle instead of blocking a connection; a client polls task state, can reconnect, and can retrieve the result later. The MCP documentation explicitly notes that client and intermediary timeouts make long-lived blocking connections impractical. Task support is extension-negotiated and host support varies.
2. **mcp-agent**: `lastmile-ai/mcp-agent` exposes orchestrator/worker and evaluator-optimizer workflows, and can move the same workflow to a Temporal execution backend for pause/resume/retry/durable history. The evaluator-optimizer pattern iterates until an evaluator accepts the result or a bounded refinement limit is reached.
3. **LangGraph**: LangGraph persists checkpoints at step/super-step boundaries for recovery and human-in-the-loop execution. Its durability guidance also recommends isolating side effects in durable tasks so resumed execution does not accidentally repeat work.

References:

- https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/extensions/tasks/overview.mdx
- https://github.com/modelcontextprotocol/ext-tasks
- https://github.com/lastmile-ai/mcp-agent
- https://github.com/lastmile-ai/mcp-agent/blob/main/examples/temporal/README.md
- https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx
- https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint

### Why v0.8.4 does not require native MCP Tasks

The 2026-07-28 protocol introduces the extension mechanism used by `io.modelcontextprotocol/tasks`, but support still varies by SDK and host. For example, the MCP Python SDK has tracked client-side task-result claiming/polling separately. DevBridge must continue to work in ChatGPT/Codex/Gemini/browser hosts that expose ordinary `tools/call` but do not advertise the Tasks extension.

v0.8.4 therefore ships a **tool-level durable fallback** now. It uses ordinary MCP tools and can later be mapped to native MCP Tasks when both sides advertise the extension. This avoids silently returning a task-shaped response to a host that cannot consume it.

## Durable run state

CodexPro stores long-run state at:

```text
.ai-bridge/long-runs/<run_id>.json
```

The JSON is bounded, schema-validated, written atomically, protected by the normal workspace path guard, and serialized per run inside the process. It records:

- stable `run_id`, workspace identity and root;
- objective and run-level acceptance criteria;
- ordered plan steps and per-step acceptance criteria;
- step status, bounded evidence and notes;
- `workRevision`, `planRevision`, and review round;
- bounded checkpoints;
- attached background `bash` task IDs;
- explicit terminal resolutions for task IDs that become unknown after a DevBridge/CodexPro process restart;
- reviewer PASS/FAIL, failed criteria and required rework;
- final completion record.

Secret-looking values are rejected from persisted plan/evidence text. State files are capped at 512 KiB, plans at 50 steps, checkpoints at 200 and review rounds at 20.

## MCP tools

The durable workflow is available in minimal, standard and full CodexPro tool modes when writes are enabled:

- `long_run_start`
- `long_run_status`
- `long_run_list`
- `long_run_update`
- `long_run_review`
- `long_run_complete`
- `long_run_cancel`

`bash` additionally accepts optional `long_run_id` and `long_run_step_id`. The task is created as an unbounded background process and the durable run is updated before control returns. If durable attachment fails, the newly-created process is cancelled instead of leaving an orphan that the plan does not know about.

## Quality gates

### Evidence gate

A step cannot move to `done` without evidence. Evidence should be concise references to observable results such as test output, a commit, an artifact digest, a runtime observation, or a review result; it should not contain credentials or raw secrets.

### Revision gate

Any meaningful work update increments `workRevision`. A PASS review records the revision it inspected. Later work makes that PASS stale, so `long_run_complete` refuses to complete until the new revision is reviewed again.

### Rework gate

A FAIL review must contain actionable `required_rework` and identify at least one failed step or failed criterion. Failed steps are reopened. A review loop is bounded at 20 rounds to prevent unbounded evaluator churn.

### Background-task gate

`long_run_review` refuses PASS while an attached task is still `running`, `cancelling`, or unknown. `long_run_complete` performs the same fail-closed check. If an MCP/CodexPro process restart loses the in-memory task registry, an old task ID becomes `unknown`; completion then requires an explicit terminal resolution and evidence instead of guessing that the task succeeded.

### Final-return gate

CodexPro server instructions tell capable model clients to create a durable long run for multi-phase or roughly >2-minute work and **not to send a final completion claim until `long_run_complete` succeeds**. v0.8.7 additionally tells the model to keep advancing in the same assistant turn while the goal remains actionable and no genuine user input, approval, or safety boundary is required; a running background task is not by itself a reason to ask the user to type “continue”.

This is an execution-discipline guardrail, not a way for the server to override ChatGPT host limits. `Connection interrupted` and `Message delivery timed out` can still originate above the MCP server in the browser↔OpenAI delivery path. The durable state exists so a reconnect or later turn can recover the exact run status without relying on chat memory, while SSE/progress liveness reduces avoidable idle-path disconnects during a still-live turn.

### Risk-tiered validation and cleanup

Creating a durable run does **not** imply that every task must run the repository's heaviest validation. The project-level `AGENTS.md` risk tier controls verification scope: documentation/process changes use focused contract/text checks and diff review; local code uses targeted regression; runtime/state changes add integration/smoke; security, routing core, installation, cross-platform and formal-release changes use the full system-level gate. This keeps long-run durability independent from test cost.

Before terminal review, clean only temporary artifacts that are both attributable to the current task and reproducible (for example one-off smoke output, temporary build directories, disposable helper scripts, or superseded transient logs). Never use cleanup as a generic repository sweeper: durable run state needed for recovery, formal release assets, user configuration, unknown-origin files, third-party source, and uncommitted work from another session must be preserved. Cleanup must be followed by a worktree/diff audit before completion.

## Timeout strategy

- `bash`: background task; no fixed execution-time limit. It ends only when the child exits, fails, or is cancelled.
- `wait_task`: bounded polling request. Without a request progress token one call is capped at 30 seconds. When the client supplies an MCP `progressToken` and the SDK exposes notification delivery, the request may wait up to 120 seconds and emits `notifications/progress` about every 8 seconds. Reaching either deadline never stops the background process.
- task transport is bounded independently from the in-memory task registry: running task stdout/stderr are projected as an approximately 2 KiB UTF-8-safe tail per stream; terminal `bash/get_task/wait_task/cancel_task` snapshots use at most about 8 KiB per stream plus omitted-byte counters, and task commands are capped to about 2 KiB UTF-8. The retained rolling buffer remains internal to `BashTaskManager`; if omitted bytes are still retained, compact task results include `detail_retrieval` and the stable `codexpro` wrapper can use `action=task_output` to page that retained/redacted stdout or stderr (8 KiB default, 16 KiB max) without increasing the default result size. If the rolling retention limit already dropped earlier bytes, the detail response explicitly marks them unavailable.
- `list_tasks` is a compact index, not a bulk log dump: it keeps every active task visible, fills the remaining budget with recent terminal tasks, returns at most 20 summaries, and omits stdout/stderr. Use `get_task(task_id)` for one task's bounded detail.
- task responses expose adaptive `poll_after_seconds` hints (5 / 15 / 60 / 120 seconds based on elapsed time).
- long-run persistence and long-run transport are separate: `.ai-bridge/long-runs/<run_id>.json` keeps the complete bounded plan/evidence/review state, while MCP status/update/review/completion responses return counts plus bounded UTF-8 tails (recent task IDs/resolutions, checkpoints, criteria/evidence/notes). When omitted history is needed, compact long-run results include `detail_retrieval`; call the stable `codexpro` wrapper with `action=run_detail` to page checkpoints, task resolutions, or one step's acceptance criteria/evidence/notes. Task/run detail cursors are opaque, process-local HMAC-sealed, bound to their object/scope/revision, and fail closed after tampering, a mutable snapshot change, or process restart. `long_run_status` exposes `next_poll_after_seconds=30` while attached work is active and separately recommends same-turn autonomous continuation when no step is blocked.
- Gateway `text/event-stream` forwarding emits a comment-only keepalive after 12 seconds of idle time, but only at complete SSE event boundaries. CodexPro HTTP uses SDK v2 `createMcpHandler` with per-request modern serving and stateless legacy fallback; protocol-session TTL/replay state is not required. Workspace/task/long-run application state survives across those stateless requests at process scope.
- `run_command` / `run_program`: remain short compatibility calls with a 20-second hard cap; builds, installs, crawls and other long work belong in `bash`.
- local `execute-handoff`: default executor timeout is 2 hours (max 24 hours).
- local `loop-handoff`: reviewer/test defaults are 1 hour and the default evaluator/rework budget is 5 iterations.
- a planned project-engine restart/hot reload must first drain active command tasks across every opened workspace owned by that project engine. If any task is `running` or `cancelling`, postpone that root restart unless the user explicitly requested cancellation; do not use process-tree termination as a routine upgrade mechanism. An unexpected crash still recovers fail-closed from durable terminal evidence.

No timeout above is treated as proof of successful work. A timeout or lost task must be reflected in persisted state and reviewed.

### Host-turn budget: what is and is not controllable

OpenAI's public ChatGPT documentation does not currently publish a fixed, contractual maximum duration for one interactive ChatGPT turn. Do not infer such a limit from one observed 20–30 minute interruption. The limits above are **DevBridge/MCP call limits**, not a documented ChatGPT-wide turn limit. OpenAI's API documentation separately recommends Background mode for model requests that may take several minutes, which confirms the general principle that request lifetime and work lifetime should be separated; that API facility is not something a local MCP server can force the ChatGPT web app to use.

DevBridge therefore uses a turn-budget discipline rather than trying to “increase” an unknown host limit: keep ordinary MCP polls at 15–30 seconds; put long work in `bash`/local executors whose process lifetime is independent of the poll request; persist a durable checkpoint at every phase boundary, before restart/install/release boundaries, and after roughly 5–10 minutes without a new durable fact; prefer `execute-handoff`/`loop-handoff` for autonomous subphases expected to run beyond about 10 minutes. Shortening individual calls reduces per-request timeout and intermediary-disconnect risk, but it cannot guarantee that the ChatGPT host will keep the same assistant turn alive indefinitely. If the host ends the turn, the durable run and background process continue to be the recovery source of truth, and the next turn resumes from `long_run_status` without asking the user to reconstruct context.

## Local executor/reviewer loop

`codexpro loop-handoff` remains useful when a local CLI agent can execute independently of the browser. v0.8.4 persists a versioned loop state containing:

- lifecycle state (`running`, `completed`, `failed`, `cancelled`);
- phase (`starting`, `executor`, `testing`, `reviewing`, `rework`, `passed`, `finished`);
- start/update/finish timestamps;
- iteration/max iteration;
- plan hash;
- executor/test/reviewer exit codes;
- reviewer verdict and rejected-PASS reason;
- terminal stop reason.

A reviewer FAIL still must produce a usable changed follow-up plan before another iteration. A reviewer PASS cannot hide a failed executor/test unless the explicit override flag is enabled.

## Recovery recipe

After a browser refresh, context compaction, connector reconnect, or client change:

1. call `long_run_list` in the intended workspace;
2. select the durable `run_id`;
3. call `long_run_status`;
4. inspect open steps, latest review, task observations, and `completion_blockers`;
5. resume only the unfinished phase;
6. review again if the work revision changed;
7. call `long_run_complete` before claiming completion.

This makes the durable plan/checkpoints the source of truth rather than an opaque transport session or a model's temporary conversation state.
