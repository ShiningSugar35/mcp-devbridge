# Changelog

All dates are local development dates.

## 0.8.9 (2026-08-29) — long-running connection governance and forensic recovery

- Harden shared Gateway/Tunnel startup and runtime recovery with bounded retry/backoff, transient DNS/network classification, cancellation-aware waits, responsibility-local restart, and Cloudflare auto→HTTP/2 fallback when connectivity evidence requires it; temporary public-path failure no longer tears down healthy local projects.
- Add a bounded, fail-open flight recorder for HTTP/MCP/SSE/OAuth lifecycle evidence: wall/monotonic time, request stages/terminal outcome, PIDs, listeners, exit codes, latency/failure counters, last-success timestamps and recent redacted Tunnel output, with strict bearer/cookie/token/password redaction and bounded disk rotation.
- Stabilize the public Hub contract at 50 tools with deterministic schema fingerprinting and call-stage permission enforcement, eliminating tools/list drift when projects start/stop or switch permission modes.
- Persist deterministic workspace-handle routing across Gateway recreation, validate canonical roots, rehydrate known read-only contexts with bounded single-flight `open_workspace`, fail closed for unknown/stale handles, and never automatically replay mutating calls.
- Preserve OAuth DCR/refresh/access-token continuity across Gateway restarts while keeping stored access validation hash-only; move slow credential-store access off the Gateway event loop so credential lookups cannot freeze `/health`, `tools/list` or other concurrent requests.
- Give Gateway upstream responses a single owner, bound HTTPX pools/deadlines/body sizes, tighten SSE keepalive/timeout/downstream-cancel cleanup, and make CodexPro shutdown stop accepting, drain/force-close active HTTP/SSE, then perform bounded handler cleanup.
- Strengthen project supervision with authenticated data-plane MCP canaries so control-plane/broker hiccups cannot restart a healthy project; project, Gateway and Tunnel recovery remain isolated by failure thresholds and cooldowns.
- Improve CodexPro analysis/search scoping and bounded single-flight/cache behavior, background task lifecycle/cancellation, nested Git handling, Windows PATH-less runtime behavior, and long-running request observability.
- Add fail-closed Windows/Linux release-version consistency checks plus expanded regression, fault-injection, lifecycle, security, resource-bound and formal soak coverage.

## 0.8.8 (2026-08-26) — explicit workspace context and authorized Windows elevation

- Make the shared Gateway a true multi-root dispatcher instead of a hidden current-workspace owner. `open_workspace` keeps the opaque CodexPro workspace handle and also returns the DevBridge route id; explicit task/path/handle evidence wins over legacy soft affinity, and bootstrap fallback is never reported as the current workspace.
- Fix relative-path routing after `open_workspace`: a valid opaque workspace handle scopes relative follow-ups to that workspace instead of re-running cross-root ambiguity resolution. Independent legacy client affinities remain bounded and isolated.
- Give `system/full_system` its intended filesystem semantics: the workspace is the default cwd/context rather than a filesystem boundary, while read-only/workspace modes retain canonical containment and system mode retains blocked-path, symlink-write, secret-redaction and destructive-command guards.
- Add a Windows elevated broker based on one explicit UAC authorization plus a `RunLevel Highest` Task Scheduler entry. The desktop remains `asInvoker`; later full-system commands and CodexPro engines run through the authenticated loopback broker without per-command UAC prompts. No UAC bypass or UAC disablement is used.
- Harden the elevation lifecycle against privilege retention and startup/stop races: permission downgrade replaces an elevated manager with a normal one, broker startup is serialized across bulk-start workers, stop failures cannot be reported as IDLE, broker child enumeration is lock-scoped, and registration elevates the controlled executable rather than executing a writable temporary script.
- Reduce routing and supervision overhead with a 350 ms active-root snapshot, an O(1) root-to-workspace index, and 250 ms elevated-status IPC coalescing. Keep workspace/affinity/request/output state explicitly bounded.
- Add routing, full-system, elevation, permission-downgrade, concurrency, negative-security and release regressions; clean historical build intermediates and keep generated `.ai-bridge` runtime state out of release commits.
- Fix the final Windows elevation live-probe defects before publication: declare pointer-sized Win32 token APIs so a real High Integrity process is not misclassified, validate the scheduled-task action against the current runtime, and grant the original user only read/execute access to the pre-authorized task while SYSTEM/Administrators retain full control.
- Make administrator setup recoverable and non-technical in the desktop UI. A successful normal-launch authorization and an already-elevated launch now converge on the same verified high-integrity path; failure offers an explicit workspace-only recovery instead of preventing MCP from starting. Ordinary UI uses user-language labels such as administrator access, access code and connection service, while implementation terms remain in diagnostics/developer docs.
- Align the CodexPro CLI with the already-supported `developer` bash mode and harden HTTP smoke child-exit handling so release tests do not fail or hang on a fast process-exit race.
- Harden final live routing: when task/path/cwd evidence overrides a stale opaque workspace handle from another active root, strip that foreign handle before proxying to the selected engine (including nested `codexpro(action,args)` calls), preventing a correctly routed call from failing with `Unknown workspace_id`.
- Make Windows live-upgrade preserve the launcher's integrity level: an already-elevated desktop creates its detached upgrade worker at Highest so it can replace the High Integrity process; ordinary launchers keep an ordinary worker. Dry-run records launcher/worker elevation for release diagnostics.

## 0.8.7 (2026-08-26) — stateless MCP and idle-502 self-healing

- Upgrade the bundled CodexPro fork from monolithic MCP SDK v1 to the stable v2 packages (`@modelcontextprotocol/client`, `@modelcontextprotocol/server`, `@modelcontextprotocol/node`) plus Zod 4, using the official codemod followed by manual API migration.
- Replace sessionful Streamable HTTP with SDK v2 `createMcpHandler(..., { legacy: "stateless" })`: 2026-07-28 traffic is served per request and 2025-era traffic uses the official stateless fallback. Remove the 30-minute protocol-session TTL, transport map, prune timer and Gateway upstream-session virtualization/replay.
- Separate protocol state from application state: workspace handles, background tasks and durable long-run coordination are process-scoped; selected-workspace and last-shown review checkpoints are isolated by bounded client-affinity state without becoming an authorization or routing authority.
- Detect exited CodexPro children instead of leaving cached READY state, add authenticated per-project `/healthz` supervision with targeted restart and no resurrection after explicit user stop, and add cooldown-protected Gateway-only recovery for local Gateway failure.
- Hold Windows `ES_SYSTEM_REQUIRED | ES_CONTINUOUS` while a public Hub is intentionally running so Modern Standby idle timeout does not suspend the local origin; never request display keep-awake.
- Change application update discovery to scan stable GitHub Releases and select the highest installable platform version, allowing direct multi-version jumps such as `0.8.4 → 0.8.7` while rejecting drafts, prereleases, malformed stable tags and releases missing the platform asset.
- Add fault-injection/stateless/update regressions and retain v0.8.6 SSE keepalive, progress-aware polling and durable long-run behavior.

## 0.8.6 (2026-08-25) — Goal-like ChatGPT long-turn resilience

- Add protocol-safe Gateway SSE idle keepalives every 12 seconds for `text/event-stream`. Keepalives are inserted only at complete SSE event boundaries, so a network chunk split can never be spliced into one JSON-RPC/SSE event.
- Add a bounded per-Streamable-HTTP-session MCP EventStore (256 events / 4 MiB / 30-minute TTL) so live sessions receive SSE event IDs and can replay retained events with `Last-Event-ID`. Oversize events remain deliverable and are represented as replay gaps rather than causing response failure or unbounded memory growth.
- Make `wait_task` liveness capability-sensitive: clients without a request progress token remain capped at 30 seconds; clients that supply standard MCP progress support may wait up to 120 seconds while receiving `notifications/progress` roughly every 8 seconds.
- Compact running `wait_task` stdout/stderr to 2 KiB UTF-8-safe tails with omitted-byte counters while retaining normal bounded terminal results; extend adaptive polling guidance to 5/15/60/120 seconds.
- Tell model clients to keep advancing in the same assistant turn while a clear goal remains actionable and no genuine user input/approval is required. Durable long-run summaries expose the same autonomy hint and distinguish blocked steps from ordinary waiting.
- Keep the host boundary explicit: these changes reduce idle-path `Connection interrupted` failures and improve reconnect recovery, but the MCP server cannot override a ChatGPT browser↔OpenAI hard turn/message-delivery timeout. Durable `long_run_*` state remains recoverable when `Message delivery timed out` ends a turn anyway.
- Add regressions for SSE idle keepalive, partial-event chunk splitting, downstream cancellation, EventStore replay/eviction/oversize gaps, request-tied progress, running-poll context pressure, and Linux shell parser validity.
- Fix a pre-existing `scripts/install_linux.sh` backtick-escaping syntax error discovered by the stronger release gate; all Linux distribution scripts now parse under Bash before release.

## 0.8.5 (2026-08-22) — ChatGPT transport timeout hardening

- Serialize shared Hub/Tunnel lifecycle startup and make same-config starts idempotent, eliminating the concurrent restore race that could spawn duplicate `cloudflared` connectors.
- Give every proxied upstream response exactly one owner: buffered branches read and close once; streaming branches forward the leased transport stream once and close it on completion/cancellation, preventing `httpx.StreamConsumed` failures.
- Add public transport health monitoring that compares local Gateway `/health` with public `/health`, records TTFB/consecutive failures, and performs cooldown-protected tunnel-only recovery when the local Gateway is healthy but the public path repeatedly degrades.
- Add regressions for concurrent shared-Hub startup, stream close/cancellation ownership, and tunnel-only recovery; preserve the v0.8.4 durable long-run architecture and v0.9.x history.

## 0.8.4 (2026-08-22) — durable long-run orchestration

- Add durable `long_run_start/status/list/update/review/complete/cancel` MCP tools for multi-phase or multi-hour work. Plans, acceptance criteria, evidence, checkpoints, review rounds and task recovery state are persisted under `.ai-bridge/long-runs/` instead of relying on a live browser request or model context.
- Require evidence before a plan step can be marked done; track work revisions so any post-review work invalidates an older PASS until the current revision is reviewed again.
- Add evaluator/rework gates: FAIL reviews must identify failed criteria and actionable rework, reopen affected steps, and stay bounded by a maximum review count.
- Attach background `bash` tasks directly to durable runs with optional `long_run_id` / `long_run_step_id`; final review/completion refuses while attached work is running/cancelling or is unknown after a process restart without explicit terminal evidence.
- Keep background tasks execution-unbounded while keeping MCP polling bounded; return adaptive 5/15/30-second polling hints so ChatGPT/Codex/browser hosts do not need a minutes/hours-long `tools/call`.
- Harden local `loop-handoff`: two-hour executor default, one-hour reviewer/test defaults, five evaluator/rework iterations, durable phase/timestamp/exit-code/terminal-reason state, and fail-closed exception recording.
- Keep native `io.modelcontextprotocol/tasks` as a future negotiated path instead of making it a baseline dependency, because current host/client support is still uneven; the ordinary-tool durable fallback works with existing MCP hosts.
- Raise the bundled CodexPro MCP SDK minimum to `@modelcontextprotocol/sdk ^1.30.0` (current resolved 1.30.0) and retain zero-known-vulnerability npm audit.

## 0.8.3 (2026-08-21) — entry-project removal hotfix

- Remove the remaining internal entry/bootstrap ownership: the shared Hub no longer owns a project CodexPro/Windows engine, Local mode routes through the shared Gateway, per-project Gateway ports are removed, and automatic fallback no longer persists a session current workspace.
- Use one Hub-scoped client Bearer and one shared Gateway port for every READY root; per-project secrets remain internal upstream credentials only.
- Remove the project-table Entry column and all service-root/bootstrap ownership paths; starting any project brings up the shared Hub if needed, stopping one project leaves every other READY root untouched, and the Hub stops only after the last root stops.
- Restore every previously running root equally after live upgrade; the legacy single `project_root` handoff field is accepted only as compatibility input and never regains priority.
- Keep `devbridge_switch_workspace` only as an explicit compatibility override. Normal path/cwd/task routing requires no switch and a single Hub session can alternate between roots without persisting an implicit current workspace.

## 0.8.2 (2026-08-21) — active-root autorouting and Linux/SteamOS recovery

- Replace the user-facing entry/current-workspace model with peer active roots: every READY project root participates in routing, and descendants inherit the root boundary without per-subdirectory registration.
- Route absolute targets to the most specific containing root; route relative targets only when the evidence is unique; reject ambiguous same-name paths instead of guessing.
- Preserve task affinity across follow-up task calls while treating opaque CodexPro workspace handles as weaker than current path/cwd evidence.
- Harden containment with canonical real paths, including `..` and symlink/junction escapes, and validate Gateway-local command/program `cwd` before execution.
- Make drive-root inventory/tree inspection tolerate inaccessible subdirectories with warnings and continue; make scoped Git operations discover the nearest nested repository.
- Remove the desktop entry column, keep all running roots active, preserve the shared Hub while any root remains, and add bulk start/stop plus bulk connection/permission settings.
- Keep the Windows Inno Setup destination-directory page available so users can choose a custom installation location.
- Restore Linux/SteamOS Desktop Mode support without reintroducing the v0.9+ multi-Agent runtime: XDG-aware config/data paths, Secret Service/AES-GCM storage, POSIX shell/process handling, safe custom user-level installation, live upgrade, PyInstaller packaging, and Ubuntu 22.04 CI/release builds.
- Keep v0.9.x branches/tags as untouched history; v0.8.2/v0.8.3 remain preserved release history; v0.8.4 is the current maintenance release line.

## 0.8.1 (2026-08-17) — stateless multi-workspace routing hotfix

- Fix ChatGPT custom-app calls falling back to the Hub entry workspace when the client recreates the underlying MCP transport session between tool-call batches.
- Add optional `devbridge_workspace_id` / `devbridge_device_id` routing hints to tool schemas; switch tools return the selected routing value and the Gateway validates/consumes it without forwarding synthetic fields to CodexPro.
- Keep `mcp-session-id` routing as a backward-compatible optimization, but no longer require it for workspace/device switching.
- Virtualize upstream MCP sessions per client-session × workspace/device. Switching from one CodexPro engine to another lazily initializes that target and rewrites the upstream `mcp-session-id`, preventing `Session not found` when independent engines are active at the same time.
- Fix Gateway-local `run_command` / `run_program` parsing of MCP `params.arguments`; cap these synchronous compatibility tools at 20 seconds and direct long builds/tests/installations to background `bash` tasks with bounded `wait_task` / `get_task` polling to reduce ChatGPT tool-stream timeout risk.
- Keep PySide async completion bridges alive until their GUI callback executes, preventing a project from reaching READY while the runtime log remains stuck at “starting”.
- Give drive-root projects such as `C:\` and `D:\` non-empty display names.
- Preserve all running project engines across detached upgrades: the updater snapshots listening project ports before terminating the old tree and the new desktop restores every previously running root equally, then brings back the shared Hub.

## 0.8.0 (2026-08-17) — persistent pairing and bundled runtime

- Extended successful registration receipts so the same `pair_code + device_id` can retry idempotently for 1800 seconds after the first successful registration.
- Persisted paired-device identity, Hub endpoint and heartbeat credentials so normal application or Windows restarts do not require pairing again.
- Update discovery now runs once on startup and then every 12 hours.
- The Windows installer bundles pinned private Node.js 22.19.0, uv/uvx 0.11.25 and cloudflared 2026.7.3 runtimes. Startup diagnostics validate the private payload instead of relying on user-global PATH state.
- Reduced synchronous `wait_task` polling to a 15-second default and a 30-second hard maximum to avoid long MCP delivery stalls in ChatGPT Web. Background command tasks still have no fixed execution-time limit.
- The v0.8.0 release gate includes Python tests, Ruff, Pyright, CodexPro build/smoke, PyInstaller, Inno Setup, frozen single-instance smoke, bundled `live_upgrade.ps1` verification, detached-updater Dry Run, installer SHA-256, live upgrade, shortcut/service recovery and fixed-domain reconnection.

## 0.7.2 (2026-08-16) — shared task registry hotfix

- Fixed a live multi-session bug from v0.7.1: `BashTaskManager` was created per `McpServer` instance, so a later MCP request/session could fail to resolve a task id returned by `bash`.
- Moved the task registry to CodexPro process scope. Tasks remain workspace-scoped inside the registry, so project isolation is preserved while all MCP sessions in the same DevBridge process can observe the same task lifecycle.
- Added HTTP regression coverage where MCP session A starts a `bash` task, closes, and MCP session B successfully waits on the same task id.
- Added a **600-second orchestration watchdog**. It never kills a task: after 600 seconds without any task observation, the next task snapshot reports `orchestrationStale=true` plus a resume hint. Any get/wait/list/cancel observation refreshes the watchdog. `wait_task` remains capped at 60 seconds per polling call.
- Full hotfix verification passed: TypeScript build, complete `npm run smoke`, `npm run stress`, 304 Python tests, Ruff and Pyright.
- Final v0.7.2 Windows installer after watchdog integration: 66,977,760 bytes, SHA-256 `09f6e87f699fdc806404a961846cf78d1ece1fbdcaff80d3a6b4a1243c577510`; frozen staging smoke and detached-upgrade dry-run passed.
- Live installed verification passed: installed process/desktop shortcut point to the normal install directory; Gateway 8786 and project engine 8788 listen; installed `server.js` contains `wait_task` and no `timeout_ms`; a separate MCP `wait_task` successfully recovered a task id returned by an earlier `bash` call; runtime snapshots expose the 600000 ms orchestration watchdog.

## 0.7.1 (2026-08-16) — timeout-free command tasks by default

- `bash` now starts every shell command as a background task and returns `task_id` immediately. The public MCP schema no longer exposes `timeout_ms` and user command tasks have no fixed execution-time limit.
- Removed the separate `start_task` tool and the normal-vs-large task distinction. Short commands and long builds now use the same execution model.
- Kept `get_task`, `wait_task`, `list_tasks`, and `cancel_task` for task lifecycle management. `wait_task` is only a bounded polling wait and never limits the underlying task.
- Task output uses a bounded rolling buffer; old output may be omitted, but output volume no longer terminates a task. MCP responses remain redacted.
- Task creation still enforces the same bash-session guard, PathGuard, safe/developer/full command policy, and dangerous-command blocks. Cancellation terminates the full process tree.
- Completed task metadata remains memory-only for up to 24 hours (maximum 100 retained terminal tasks). Running tasks are never evicted by retention cleanup. DevBridge/CodexPro restart intentionally ends running tasks and clears task state.
- Restored the complete CodexPro Windows smoke chain by aligning stale Tool Cards, connection-test, doctor/settings and execute-handoff expectations with current behavior.
- Verification before packaging: 304 Python tests passed; Ruff clean; Pyright 0/0; TypeScript build green; full `npm run smoke` green, including real MCP `bash → wait_task` and cancel-process-tree coverage.

## 0.7.0 (2026-08-13) — Multi-Device Hub and beginner-facing UX

- Added a device registry with stable local identity, one-time pairing codes, encrypted per-device Bearer/heartbeat credentials and heartbeat-based presence.
- Added Hub routes `/device/register` and `/device/heartbeat`; remote Quick Tunnel URL changes are learned automatically from heartbeats.
- Added session-isolated `devbridge_list_devices`, `devbridge_get_current_device` and `devbridge_switch_device`. A single online device is selected automatically; multiple online devices keep local as the default until explicitly switched.
- Remote device selection transparently proxies MCP traffic to that device while its own workspace tools continue to control projects on that computer. Tool injection is de-duplicated for Hub-to-Hub proxying.
- Fixed process logs to read the selected project's real engine log instead of treating `ProcessLog` as an iterable or pinning the public-entry project.
- Fixed audit logs by recording the actual Gateway → CodexPro `tools/call` path; audit parameters remain redacted by the existing `AuditLogger` policy.
- Reworked Logs to beginner-facing Run Status / Operation History / Network Connection views and Diagnostics to a conclusion-first, next-action format.
- Removed the duplicate connection-test card and component jargon from Workbench; self-test and technical component state now live under Diagnostics.
- Added searchable in-app Manual, connection advisor and contextual `?` help for connection info, Gateway port, connection method, public hostname and tunnel token.
- Quick Tunnel behavior is documented as random `trycloudflare.com`, test/development only; fixed-address Hub is recommended for long-term multi-device use.
- Verification: **304 pytest cases**, Ruff clean, Pyright 0 errors / 0 warnings, multi-device routing integration green, all top-level pages fit 900×650 / 1200×850 / 1920×1080, frozen staging smoke and detached-upgrade dry-run passed, final installer asset SHA-256 was verified, and the installed v0.7 build restored Gateway 8786 / Codex 8788 and successfully served `devbridge_list_devices`.

## 0.6.0 (2026-08-11) — desktop UX and per-project interaction isolation

- Reworked the top-level information architecture into Workbench / Project Settings / Diagnostics / Logs / Settings; process, audit and Gateway logs now live under one Logs section.
- Replaced the global desktop busy lock with per-project operation state. A running project no longer disables idle projects; its own stop action remains available while only non-hot settings for that project are locked.
- Reduced persistent helper copy and moved technical detail into tooltips, diagnostics and advanced settings.
- Fresh-install UI no longer surfaces any developer-machine project/path/domain/Git/Gemini values; connection fields stay empty until the user adds a project. Existing generic port allocation remains configurable.
- Added system-tray behavior: title-bar minimize remains a normal taskbar minimize; close defaults to hiding in the tray, with restore/exit tray actions and a persisted close-behavior setting.
- Added v0.6 regressions for clean defaults, project-isolated controls and tray close behavior.
- Verification: 294 pytest cases, Ruff clean, Pyright 0 errors / 0 warnings, responsive/offscreen/tray regressions green, frozen staging smoke passed, final Inno installer silent-install returned 0, and detached live upgrade restored the installed build with Gateway 8786 / Codex 8788 ready.

## 0.5.0 (2026-08-10) — per-project desktop configuration and live-upgrade safety

- Six-column project table; removed the `enabled` column/desktop auto-restore behavior; 1-second real state refresh and dynamic row start/stop action.
- Permission/client/connection combos ignore wheel input; service control is a single dynamic start/stop button.
- New projects default to `system + full_system` (“full access / dangerous”); the one-time first-use risk acknowledgement remains.
- Per-project client target, connection, hostname, Git settings, Gateway/Codex/Windows ports, Gemini redirect URI, Bearer and Cloudflare tunnel values.
- Gemini OAuth panel is conditional on the Gemini client target. Consent now blocks missing/stopped workspaces instead of issuing a code.
- Restored Cloudflare Named, ngrok fixed, Quick Tunnel and Local modes. Public tunnels target the Gateway; Local requires no cloudflared; Quick/ngrok URLs normalize to `/mcp`.
- Gateway routes direct project Bearers and OAuth workspace sessions to the correct running engine and swaps in that project's upstream credential.
- Added component status, one-click connection diagnostics, project-specific real self-test results, background window shutdown and upgrade-resume handling.
- Builds use versioned staging directories, allowing 0.5.0 to be built while 0.4.0 is still running from the old dist tree; the staging package includes `cloudflared.exe` beside the frozen executable and frozen lookup prefers that packaged copy.
- Verification: 289 pytest cases, Ruff clean, Pyright 0/0, Qt offscreen smoke, PyInstaller staging build and Inno Setup installer build.

## 0.2.0-dev (2026-08-09) — port config + bridge hardening

- **Unified port configuration**: single source of truth in
  `constants.DEFAULT_GATEWAY_PORT/DEFAULT_CODEXPRO_PORT/DEFAULT_WINDOWS_MCP_PORT/
  DEFAULT_LEGACY_BACKEND_PORT` (8786/8787/28731/8765). `AppConfig` gains four
  persisted port fields (1-65535 validation, old-config migration);
  `RuntimeConfig.local_port` → compat property over `legacy_backend_port`;
  ambiguous `ProjectConfig.local_port` removed.
- **Desktop UI**: Gateway port spinbox + check port / restore default /
  copy Service URL + prominent warning to sync the Cloudflare Service URL;
  「高级设置…」dialog for the four internal ports; inputs locked while the
  service runs; startup pre-check refuses occupied ports (no silent retry).
- **Windows-MCP pinned** to `0.8.2` (`uvx --from windows-mcp==0.8.2 …`
  via `engines.WINDOWS_MCP_PINNED_VERSION`).
- **Bridge tool allowlist enforcement**: `CODEXPRO_WINDOWS_PROFILE` derived
  from the permission mode (`desktop_ui` allowlist vs `system_full`),
  enforced in `windowsBridge.ts` against the live bridge inventory.
- **Build & CI**: rewritten `scripts/build.ps1` (UTF-8, full chain
  pytest→ruff→pyright→PyInstaller→ISCC + version consistency); GitHub Actions
  `ci.yml` + `release.yml`; root `LICENSE` (MIT) and updated
  `THIRD_PARTY_LICENSES.md` (Windows-MCP ≥0.8.2, cloudflared, desktop runtime).
- New tests: `tests/test_port_config.py` (16).

## 0.1.0 — first release (in progress)

### Phase 0–1 (2026-08-06) — core + 33 tools
- Project scaffolding: pyproject/hatchling, venv lock, config store, secrets
  (Win Credential Manager + DPAPI fallback), audit logger with redaction,
  permission model (read_only / workspace / system).
- 33 MCP tools across file ops, commands, git, process, env, sysinfo.

### Phase 2 (2026-08-06) — HTTP + auth + subprocess
- Python `server_factory` (MCPServer + Starlette), bearer auth, rate limiter,
  `/health` + `/control/*`, backend CLI (`server_main`, `standalone_server`).

### Phase 3 (2026-08-08) — desktop
- PySide6 single window; ServiceCoordinator order (engine/window/tunnel);
  connection combo (local / cloudflare / ngrok / quick), tunnel token input,
  auto-generated Windows bridge token, fixed vs mutable URL indicator,
  QThreadPool to keep the UI responsive.

### Phase 4 (2026-08-08) — public tunnel (real device verified)
- `tunnel_manager` runs `cloudflared tunnel run --token <..>` (fixed
  `--no-autoupdate` subcommand regression); route/DNS verified at CF edge;
  E2E `https://mcp.shiningsugar.shop/mcp` → 401 (expected auth challenge).
- `server_factory.build_transport_security()` — DNS rebinding protection ON;
  `allowed_hosts` += public hostname (方案 B). `RuntimeConfig.public_hostname`
  wired (config + `standalone --public-hostname`); 12 new tests.

### Phase 5 (2026-08-08) — Git desktop settings
- `ProjectConfig` + `git_user_name/email/default_push_remote/branch` (empty
  allowed, backward compatible); `models.git_field_error()` Chinese validator;
  desktop Git panel with save + startup validation; 8 tests.

### Phase 6 (2026-08-08) — logs
- `audit.query_logs / AuditQuery / available_tool_names`; three-tab UI
  (control / process logs / audit); rotation (14 days / 50 MB) exercised;
  **fix** redaction marker matching (`api_key` etc. now masked); 9 tests →
  total **154 green**.

### Phase 7 (2026-08-08) — packaging & docs (completed)
- PyInstaller onedir: `packaging/local-dev-mcp-bridge.spec`, `scripts/build.ps1`;
  built `dist\LocalDevMCPBridge\` (cloudflared.exe bundled, launch smoke OK).
- Inno Setup 6.7.3: `scripts/installer.iss` (per-user, no admin/UAC) →
  `release/LocalDevMCPBridge-Setup-0.1.0.exe`; silent install EXIT=0, installed
  app launch smoke OK.
- English docs added (ARCHITECTURE / SECURITY / COMPATIBILITY / DEVELOPMENT /
  this CHANGELOG); Chinese docs updated.
- Remaining: GUI loop (pick project → start → self-test) to be run interactively.
- **Fix** packaged app crash: frozen `desktop_main.py` failed with
  `ImportError: attempted relative import with no known parent package`;
  added `packaging/entry_desktop.py` absolute-import wrapper as the spec entry;
  rebuilt, recompiled installer, reinstalled — launch smoke OK.
- **Fix** "CodexPro build missing" in packaged app: module-relative dist path no
  longer resolves after freezing. Added `CodexProManager._resolve_dist_dir()`
  candidate chain (env `CODEXPRO_DIST_DIR` → bundled `_MEIPASS` copy → source
  tree → project-root `third_party/codexpro/dist` → ProgramData) and bundled
  the codexpro dist in the spec `datas` (verified present in installed app).
- Removed tunnel-token copy/paste across restarts: last-used Cloudflare tunnel
  token is remembered (set when typed, prefilled on next launch) via
  `LocalDevMCPBridge/CloudflareTunnelToken` in the secret store.
- Full suite still 154 tests green.
- **Fix** engine crash in packaged build: `ERR_MODULE_NOT_FOUND: express` —
  bundled `dist` had no sibling `node_modules` (ESM deps resolve up the tree).
  Bundled `third_party/codexpro/node_modules` in the spec and made
  `_find_http_js()` prefer dists that have node_modules (explicit dir stays
  authoritative). Verified with the installed artifact: `node http.js` logs
  `[CodexPro] HTTP MCP listening on ...`. Installer reinstalled — engine boots.

### Phase 8 (2026-08-08) — MCP OAuth (Gemini-compatible)
- New `oauth_provider.py`: `LocalOAuthProvider` on the MCP SDK
  `OAuthAuthorizationServerProvider` (single-user, no user registry).
  Dynamic Client Registration accepts only the `ACCESS_VIEW_MANAGE_MCP_CONTENT`
  scope; consent window 10 min; single-use 5-min authorization codes;
  1-hour access tokens; rotating 60-day refresh tokens; RFC 8707 resource
  binding; RFC 7009 revocation. Client registrations persist encrypted via
  SecretsStore (DPAPI / Credential Manager); codes & access tokens stay in
  memory only.
- New `gateway.py`: uvicorn OAuth gateway on `127.0.0.1:8786`
  (`GATEWAY_PORT`). Exposes SDK OAuth routes (discovery metadata, /authorize,
  /token, /register, /revoke), protected-resource metadata, a browser consent
  page at /consent, /health, and a `/mcp` reverse proxy: valid OAuth access
  token → engine Bearer (same secrets source); legacy ChatGPT Bearer →
  constant-time pass-through; loopback → anonymous allow; otherwise 401 with
  `resource_metadata` in WWW-Authenticate. The gateway writes no audit logs:
  no token ever touches disk in plaintext.
- `ServiceCoordinator` starts the gateway only in non-local connection mode
  (after Codex ready, port probe on 8786), stops it before the tunnel, and
  exposes a `gateway` component state.
- Dependency: `httpx>=0.28` added to runtime deps; packaging spec gains
  `oauth_provider` / `gateway` hidden imports.
- Tests: new `tests/test_oauth.py` (27 cases) — discovery, DCR (public +
  confidential), consent allow/deny/expiry, PKCE mismatch, code single-use,
  refresh rotation/expiry/revoke, proxy behavior (OAuth forward, legacy
  pass-through, 401 paths, upstream 5xx pass-through), and no-plaintext
  tokens on disk. Full suite: **181 tests green**.

## Known issues
- Real-device Smoke Test results apply to the verified Cloudflare setup
  (mcp.shiningsugar.shop); other zones require the same route steps.
