# Architecture

This document describes the current v0.8.6 maintenance line. Historical implementation details belong in `CHANGELOG.md`, not in the live architecture contract.

## Runtime model

In public modes, MCP DevBridge exposes one Hub/Gateway address while any number of project roots may be running at the same time:

```text
ChatGPT / Gemini
        │ Streamable HTTP /mcp
        ▼
Cloudflare Named / ngrok / Quick Tunnel
        ▼
OAuth/Bearer Gateway (loopback)
        │
        ├── active root A → CodexPro A
        ├── active root B → CodexPro B
        ├── active root N → CodexPro N
        └── optional remote device → remote DevBridge Hub
                                      └── its own active roots
```

Every READY project root is active and equal for routing. The shared Gateway/Tunnel lifecycle is independent from all project roots: ServiceCoordinator owns only shared transport, while ProjectManager owns every project engine.

`Local` mode uses the same shared loopback Gateway and multi-root routing layer, but does not start a public Tunnel. There is no selected-project CodexPro client endpoint in the normal workflow.

## Active-root routing

The Gateway derives a target root from the current tool call rather than requiring a prior workspace switch.

Routing evidence is applied in this order:

1. explicit DevBridge routing override, retained for compatibility and diagnostics;
2. task affinity for a previously-created `task_id`;
3. path/cwd/root/target/patch/command evidence in the current call;
4. opaque CodexPro `workspace_id=ws_...` affinity when no stronger evidence is present;
5. a deterministic running root as the bootstrap fallback for calls with no path evidence.

Absolute paths are canonicalized and routed to the most specific running root that contains them. If both `D:\` and `D:\Environment\mcp` are active, a descendant of the latter routes to that nested root.

Relative paths auto-route only when the evidence identifies exactly one active root. If multiple active roots contain an equally valid `README.md`, `src/`, or other relative target, the Gateway rejects the call as ambiguous and asks for an absolute path instead of guessing from root length, registration order, or bootstrap state.

A single call that provides strong path evidence belonging to multiple active roots is rejected or must be split into separate calls.

## Path safety

Automatic routing does not weaken filesystem boundaries.

- Containment uses canonical/real-path semantics rather than string-prefix checks.
- `..`, POSIX symlinks, and Windows junctions/symlinks cannot escape the selected running root.
- Gateway-local `run_command` / `run_program` validates the requested `cwd` against the selected root.
- Root-drive inventory and tree scans treat `EACCES` / `EPERM` subdirectories as warnings and continue scanning.
- Scoped Git tools discover the nearest enclosing Git repository from the target path, so a drive root itself does not need to be a repository.

## Projects and desktop lifecycle

`ProjectManager` owns the non-secret project catalog and one `ProjectUnit` per root. Each unit has independent CodexPro/Windows-MCP processes and project ports.

The desktop project table has five columns:

```text
Name | Path | Status | Port | Action
```

Per-project busy state prevents concurrent start/stop races without globally disabling other projects. Row action widgets are reused across the one-second status refresh rather than being destroyed while the user is clicking them.

“Start all projects” and “Stop all projects” provide bulk lifecycle control. Stopping one project stops only its `ProjectUnit`; if another active root remains, the shared Hub/Gateway/Tunnel stays online. The shared network path is stopped after the last running root is stopped.

The historical `enabled` configuration field remains only for backward-compatible config/API parsing; runtime activity is determined by the live project state.

## Shell task model

Public CodexPro shell execution is task-based:

```text
bash(command)
  → policy/path checks
  → spawn background process
  → return task_id immediately
  → get_task / wait_task / list_tasks / cancel_task
```

`bash` has no public execution timeout. `wait_task` limits only one polling wait and never limits the child process. `BashTaskManager` is process-scoped so task IDs survive MCP transport/session replacement while still being workspace-scoped internally. Gateway task affinity keeps follow-up task calls on the engine that created the task.

Output is kept in a bounded rolling buffer. Cancellation terminates the process tree. A 600-second orchestration watchdog only annotates a stale task snapshot with a resume hint; it never terminates the task. Running tasks are not resumed across a DevBridge/CodexPro process restart.

## Durable long-run orchestration

v0.8.6 layers a durable plan/evaluator state machine above the process-scoped shell task manager. Multi-phase or roughly >2-minute work should call `long_run_start`, persist objective/steps/acceptance criteria, checkpoint evidence with `long_run_update`, attach background `bash` work to the run, then pass a `long_run_review` before `long_run_complete`.

The state file `.ai-bridge/long-runs/<run_id>.json` is schema-bounded, atomically replaced, path-guarded, serialized per run inside the process, and rejects secret-looking persisted text. `workRevision` changes whenever meaningful work changes; a PASS review is only valid for the revision it inspected. A later mutation makes that PASS stale. FAIL reviews require explicit failed criteria/rework and reopen affected steps.

Attached tasks gate review/completion. Running/cancelling work blocks PASS. After a CodexPro process restart an old task id may be unknown because command processes are intentionally not resumed; completion then fails closed until an explicit terminal resolution with evidence is persisted. This avoids interpreting lost process state as success.

MCP polling remains bounded but capability-sensitive: without progress-notification support one `wait_task` is capped at 30 seconds; with a request `progressToken` it may wait up to 120 seconds and emits standard progress about every 8 seconds. Running poll payloads carry only small output tails, while adaptive hints stretch from 5 to 120 seconds as task age grows. The Gateway also emits 12-second comment keepalives on otherwise-idle SSE streams only at complete event boundaries, and each live CodexPro HTTP session has a bounded EventStore for SSE event IDs / `Last-Event-ID` replay. The baseline protocol surface still uses ordinary tools for host compatibility; native `io.modelcontextprotocol/tasks` remains a future capability-negotiated mapping. See `LONG_RUNNING_TASKS.md` and `GOAL_LONG_RUNNING_TASKS.md`.

## Multi-device routing

The Hub owns a `DeviceRegistry`. Remote DevBridge instances expose an HTTPS MCP endpoint, pair with a short-lived code, and heartbeat their current endpoint. `devices.json` contains only non-secret metadata; remote Bearer and heartbeat credentials remain in `SecretsStore`.

Device selection and active-root selection are separate layers:

```text
client
  → device routing
  → active-root routing on that device
  → tool execution
```

A remote Quick Tunnel address may change and be updated through heartbeats without changing the ChatGPT-facing main Hub URL. Hub-to-Hub tool augmentation is name-deduplicated.

## Authentication and secrets

Public modes terminate at the OAuth/Bearer Gateway. OAuth authorizes the Hub; the consent page does not require choosing an “entry workspace”. The Gateway selects the concrete active root at tool-call time and uses that project’s upstream credential when proxying to CodexPro.

Windows secret storage prefers Windows Credential Manager with the existing DPAPI fallback. Linux/SteamOS prefers the desktop secret service and falls back to an AES-GCM encrypted user-level store with user-only key/file permissions. Secrets are not plaintext fields in `projects.json`, URLs, audit logs, or upgrade-resume metadata.

## Core components

| Module | Responsibility |
|---|---|
| `desktop_main.py` | PySide6 desktop UI, five-column project table, per-project/bulk lifecycle, settings, diagnostics, logs and upgrade handoff. |
| `project_manager.py` | Project catalog, per-root `ProjectUnit`, independent process/port lifecycle. |
| `app_state.py` | Shared Hub Tunnel/Gateway transport orchestration only; it does not own project CodexPro/Windows engines. |
| `gateway.py` | OAuth/Bearer Gateway, active-root/task/device routing, upstream MCP session virtualization and auditing. |
| `platform_support.py` | Windows/POSIX process differences, XDG paths, user install locations and desktop integration helpers. |
| `secrets.py` | Windows Credential/DPAPI and Linux secret-service/AES-GCM secret storage. |
| `update_manager.py` | GitHub Release discovery, platform asset selection and live-upgrade handoff. |
| `third_party/codexpro/src/longRunOps.ts` | Durable plan/checkpoint/evidence/review/rework/completion state machine for long-running work. |
| `third_party/codexpro/` | File/Git/shell/task/code-analysis MCP engine fork. |
| `scripts/build.ps1` | Windows release gate, PyInstaller and Inno Setup. |
| `scripts/build_linux.sh` | Linux test/smoke/frozen build and tarball packaging. |

## Platform and upgrade model

Windows releases are per-user Inno Setup installations and keep the directory selection page enabled. Frozen packages include private runtime payloads required by the desktop.

Linux/SteamOS releases install into a user-writable directory (default `~/.local/opt/MCPDevBridge`) and respect valid absolute `XDG_CONFIG_HOME` / `XDG_DATA_HOME` values. Relative XDG base-directory overrides are treated as invalid and fall back to the standard user locations. Linux installation refuses dangerous target roots and refuses to overwrite unrelated non-empty directories.

Both platforms use detached live-upgrade helpers. Upgrade-resume files contain only non-secret metadata; the restarted application reloads protected values from `SecretsStore` before restoring services.


### No entry port or entry credential

The Hub has one global Gateway port and one client-facing bearer. Per-project CodexPro/Windows-MCP ports and upstream credentials are internal only. Legacy per-project `gateway_port` values are ignored during config loading.