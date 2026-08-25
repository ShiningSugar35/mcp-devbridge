# Goal-like long-running work in ChatGPT (v0.8.6)

## Objective

MCP DevBridge cannot control the lifetime of ChatGPT's browser-to-OpenAI answer stream or force the host to create another model turn after that turn ends. v0.8.6 therefore targets the strongest server-side guarantee that is actually implementable: **keep an active ChatGPT MCP turn busy and observable for as long as the host permits, while making every local execution and orchestration checkpoint recoverable if the transport still reconnects.**

The desired user experience is Codex Goal-like: after the user gives a sufficiently clear goal, normal waiting, testing, review, rework and release phases continue without asking the user to type “continue”. A user response is required only for a real product/safety/approval/input boundary or when the host itself ends the turn.

## Evidence base

The design combines patterns that converge across current platform and workflow systems:

- OpenAI long-running guidance separates the long-running computation from one fragile foreground connection. Background Responses can keep running after a stream drops and can be reattached with a sequence cursor; Codex Goal mode similarly treats the goal as the durable unit of work rather than a single synchronous command.
- MCP Streamable HTTP supports request-scoped/server notification streams, SSE event IDs and `Last-Event-ID` replay. The official TypeScript SDK exposes `EventStore` specifically for reconnect/resume.
- MCP progress notifications are the standard way for a long request to emit incremental activity when the client supplies a progress token.
- SSE comment frames are protocol-safe keepalives: clients ignore them, but they keep an otherwise-idle HTTP stream active.
- Temporal and LangGraph both separate durable orchestration state from retryable side effects, checkpoint between steps, and require idempotent/recoverable work rather than relying on a permanently-open socket.

## Constraints

1. The ChatGPT web host owns the model-turn/tool-call budget and browser stream. DevBridge can reduce avoidable transport idle and payload pressure but cannot override a host-side hard limit.
2. The v0.8 maintenance line must not reintroduce the removed v0.9 multi-Agent runtime. Goal-like behavior is a **single durable run + background tasks + transport continuity** contract.
3. `execute-handoff` / `loop-handoff` remain local CLI execution features. They are not remotely wrapped merely to imitate Goal mode; doing that would change the remote execution/security boundary.
4. Native MCP Tasks remain capability-negotiated/experimental. Ordinary `tools/call` + durable `long_run_*` stays the compatibility baseline.

## v0.8.6 design

### A. Request and stream liveness

- Gateway SSE responses use a 12-second idle keepalive wrapper. DevBridge emits a comment-only frame only while the stream is at a complete SSE event boundary, so a JSON-RPC/SSE event split across network chunks can never be spliced by the keepalive. Upstream response ownership stays single-consumer.
- `wait_task` emits request-tied progress notifications only when the MCP client supplied a `progressToken`. Without progress support one wait is capped at 30 seconds; with it, a request can wait up to 120 seconds and emits progress roughly every 8 seconds instead of leaving the request completely silent.
- The long-lived GET notification stream is protected by the same Gateway SSE keepalive path.

### B. Reconnect/resume

- Each CodexPro Streamable HTTP session gets a bounded in-memory MCP `EventStore`. SSE messages receive event IDs and a reconnect carrying `Last-Event-ID` can replay missed events while the server/session is still alive.
- The store is intentionally bounded by count, bytes and TTL so a noisy client cannot convert resumability into unbounded memory growth. A single event larger than the replay byte budget is delivered normally but stored as a replay-gap marker; replay crossing that gap fails closed instead of failing the original tool response.
- Durable `.ai-bridge/long-runs/<run_id>.json` remains the cross-turn source of truth. Event replay improves transport continuity; it does not replace durable workflow state.

### C. Same-turn autonomous continuation

- Server instructions explicitly distinguish **bounded tool request** from **assistant-turn autonomy**: when a clear user goal is still actionable and no real user input/approval is required, the model should continue in the same turn rather than returning early and asking the user to say “continue”.
- During long tool-heavy periods the model is instructed to emit concise user-visible progress updates before continuing. This keeps the user informed and gives the ChatGPT answer stream regular model output instead of minutes of tool-card-only silence.
- `wait_task` running results explicitly say that the user does not need to reply and the assistant should continue automatically.
- Long-run summaries expose an autonomous-continuation hint while a run is still working.

### D. Poll payload control

Repeated polls must not resend a large rolling stdout/stderr buffer into model context. Running `wait_task` responses therefore expose only a small output tail in structured content. Terminal results retain the complete bounded task snapshot. `get_task` remains available when the model explicitly needs the current full rolling buffer.

## Software and hardware feasibility

The target Windows machine is an HP ProBook 450 G8 with an Intel i5-1135G7 (4 cores / 8 logical processors) and about 15.7 GiB RAM. The added work is transport bookkeeping rather than compute-heavy processing:

- an SSE keepalive is only a tiny comment frame every few seconds on active streams;
- one bounded event store is capped to a few MiB and pruned by TTL/count;
- compact poll payloads reduce, rather than increase, model/context/network pressure;
- no always-on local LLM, database server, container runtime or additional Agent process is required.

Therefore the design is feasible on the current hardware. The primary residual risk is host-side ChatGPT turn policy, which cannot be eliminated by local software.

## Release acceptance criteria

### Transport

- SSE helper forwards upstream payload byte-for-byte in order, emits keepalive comments only after an idle interval, and closes the upstream exactly once on completion or downstream cancellation.
- Non-SSE responses never receive SSE comments.
- A client reconnect using `Last-Event-ID` can replay stored session events; event storage remains within configured count/byte/TTL bounds.
- `wait_task` progress notification logic never invents a progress token and never changes task execution lifetime.

### Autonomy and context pressure

- Server instructions explicitly say not to ask for a manual “continue” while a clear goal remains actionable.
- Running `wait_task` structured output is bounded to a small tail; terminal task output remains available.
- Existing `bash`, `long_run_*`, multi-root routing and short compatibility commands remain backward compatible.

### Fault injection

Automated tests must cover at least: idle SSE keepalive, ordinary SSE ordering, downstream cancellation/close, non-SSE passthrough, event-store eviction/replay, running-vs-terminal task transport payload, and long-run autonomous-continuation hints. Existing Gateway `StreamConsumed` regression must remain green.

### Release gates

Ruff, Pyright, full pytest, CodexPro TypeScript build, complete smoke suite, dependency audit, lock checks, release-script syntax checks and Windows release build must pass. GitHub release jobs must produce Windows and Ubuntu 22.04 assets from the exact release commit. A defect-first review after implementation is a hard gate: any actionable finding reopens implementation and requires re-test/re-review before tag/release.

## Primary references

- OpenAI API: background mode and resumable response retrieval/stream cursors — https://developers.openai.com/api/reference/resources/responses and https://developers.openai.com/api/reference/resources/responses/methods/retrieve
- MCP specification: Streamable HTTP, progress tokens/`notifications/progress`, and Tasks polling — https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks and https://modelcontextprotocol.io/specification/2025-11-25/schema
- Cloudflare Tunnel: QUIC/HTTP2 troubleshooting, idle long-lived sessions, and connectivity pre-checks — https://developers.cloudflare.com/tunnel/troubleshooting/ and https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/troubleshoot-tunnels/common-errors/
- Durable workflow patterns used only as architectural analogies: Temporal durable execution and LangGraph persistence/checkpointing.
