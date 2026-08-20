# Architecture

## v0.10.0 persistent Agent runtime

Agent execution has three explicit layers. `AgentPool` remains a bounded primitive for one executor process and its worktree. `AgentOrchestrator` owns logical Agents and Teams. `AgentRuntimeLoop` owns the durable user objective across any number of executor processes and application restarts:

```text
Task Create → ObjectivePlanner → Execution Turn → Checkpoint
       ↑                                      ↓
       └──── Continue ← CompletionValidator ←┘
                              │
                         Finish / waiting_human
```

Each objective has an atomic on-disk `TaskState` containing the task id, objective checklist, completed items, stage, iteration/retry counters, workspace route, current/previous turns, evidence and latest checkpoint. Every turn records tool/evidence results, modified files, an output summary and the next plan. JSONL `agent_runtime_logs` explain every continuation, validation failure, retry, restart recovery and human pause.

An executor's prose or `status=success` is not a completion authority. `CompletionValidator` requires every checklist item plus a verified receipt and checks the evidence implied by the objective: Git/file changes, tests, build artifacts, executable presence, service/MCP health, commits and pushes. Failed validation creates another turn automatically on the same task id/workspace/worktree. Provider and tool failures retry with exponential backoff; repeated failures enter `waiting_human`. A later `message_agent` response resumes that same checkpoint rather than creating a replacement task.

At Gateway startup, persisted `queued`, `running`, or `interrupted` objectives cause eager Runtime initialization. The old executor process is correctly recorded as interrupted; a new continuation turn inherits its task id, checkpoint, checklist, route workspace id and previous output.

## v0.7.1 command task model

Public shell execution is task-based by default. The `bash` MCP tool validates the command and workspace, spawns it through `BashTaskManager`, and immediately returns `task_id`; it has no public timeout argument or execution timer. The `BashTaskManager` is process-scoped (not `McpServer`/MCP-session scoped) so task ids remain resolvable across successive HTTP MCP sessions while each task is still bound to its workspace id. `get_task`, `wait_task`, `list_tasks`, and `cancel_task` manage the task lifecycle. `wait_task` only bounds a polling call, not execution. Output is held in a bounded rolling buffer, cancellation terminates the process tree, and all normal PathGuard/bash-session/permission checks remain in force. Terminal task metadata is memory-only and expires after 24 hours; restarting DevBridge/CodexPro ends running jobs and clears the registry.
The task registry also tracks the last client observation. If no get/wait/list/cancel observation occurs for 600 seconds, the next snapshot reports an orchestration-stale flag and resume hint. This watchdog is advisory only and never changes task status or terminates the process.

## v0.7 multi-device routing
## v0.7 multi-device routing

The public Hub Gateway owns a `DeviceRegistry`. Remote DevBridge instances first expose their own MCP endpoint through Named Tunnel, ngrok or Quick Tunnel, then pair to the Hub with a short-lived one-time code. The Hub persists only non-secret device metadata in `devices.json`; remote Bearer and heartbeat credentials remain in `SecretsStore`.

Each MCP session has an independent device binding in addition to its workspace binding. Device-local routing behaves as follows:

1. if exactly one registered/local device is online, use it automatically;
2. with multiple devices online, keep local as the default unless that session explicitly switches;
3. Hub-owned device tools always execute on the Hub;
4. workspace/command/file tools proxy to the selected remote DevBridge when a remote device is selected;
5. the remote DevBridge then applies its own session-to-workspace mapping and permission policy.

Remote devices heartbeat about every 15 seconds and report the current public MCP URL. This makes a remote Quick Tunnel address replaceable without changing the ChatGPT-facing Hub URL. `tools/list` augmentation is name-deduplicated so Hub-to-Hub routing does not duplicate DevBridge tools.

## v0.6 desktop interaction layer

Desktop operation state is per-project rather than global. Only the project currently transitioning is busy; READY projects retain their stop action and other IDLE/ERROR projects remain selectable and startable. The desktop navigation is Workbench / Project Settings / Diagnostics / Logs / Settings, with log sub-tabs for process, audit and Gateway output. `AppConfig.close_behavior` controls close-to-tray (default) versus direct exit; the normal minimize button still minimizes to the taskbar.

## v0.5.0 overview

```text
ChatGPT / Gemini Spark
        │ HTTPS MCP (/mcp)
        ▼
Cloudflare / ngrok / Quick Tunnel
        │ (public modes target the selected project's Gateway port)
        ▼
OAuth/Bearer Gateway (loopback)
        │ per-session / per-workspace routing
        ├── Project A CodexPro + optional Windows-MCP
        ├── Project B CodexPro + optional Windows-MCP
        └── Project N CodexPro + optional Windows-MCP
```

Local mode skips the public tunnel and Gateway and connects directly to the selected project's loopback CodexPro endpoint.

## Core components

| Module | Responsibility |
|---|---|
| `desktop_main.py` | PySide6 UI: six-column project table, per-project settings, client selector, four connection methods, dynamic start/stop, component state, diagnostics, logs and upgrade handoff. |
| `project_manager.py` | Project catalog and per-project `ProjectUnit`; independent CodexPro/Windows/Gateway port allocation and parallel engine lifecycle. |
| `project_secrets.py` | Per-project encrypted Bearer and Cloudflare tunnel values with backward-compatible legacy migration. |
| `app_state.py` | Full-entry `ServiceCoordinator`; public tunnel → Gateway, engine/gateway/bridge readiness, failure cleanup. |
| `gateway.py` | OAuth 2.1 + Bearer reverse proxy; session/workspace routing; per-project upstream credential selection; Gemini consent workspace gate. |
| `agent_runtime.py` | Durable `TaskState`, objective planning, checkpoints/traces, completion validation, automatic continuation, retry and restart/human resume. |
| `agent_orchestrator.py` | Connects the Runtime to AgentPool turns and manages logical Agents plus worker/reviewer/merger Teams. |
| `agent_pool.py` | Bounded one-turn executor queue, process cancellation, verified receipts and Git worktree/direct isolation. |
| `tunnel_manager.py` | Cloudflare Named, ngrok reserved domain, Quick Tunnel and Local modes. Quick/ngrok/fixed public URLs normalize to `/mcp`. |
| `models.py` | `ProjectConfig`, including permission, client target, connection, per-project ports, Git and Gemini redirect URI. |

## Multi-project state

`projects.json` stores non-sensitive project settings. Sensitive values never enter that JSON: project Bearers and Cloudflare tokens are stored through Windows Credential Manager or the DPAPI fallback. One project can own the full public entry while other project engines remain live; the Gateway routes MCP sessions to the requested running workspace.

The desktop polls state every second. Entry-project state comes from `ServiceCoordinator`; other project rows come from their `ProjectUnit`, preventing a connected project from remaining visually stuck at “未启动”.

## Upgrade handoff

Builds use versioned `dist/staging-<version>` directories so a running older executable never blocks PyInstaller cleanup. A detached updater may write `%LOCALAPPDATA%\LocalDevMCPBridge\upgrade-resume.json` with non-secret project metadata. The new desktop consumes it after startup, reloads credentials from SecretsStore and restores the service when prior risk acknowledgement allows unattended startup.
