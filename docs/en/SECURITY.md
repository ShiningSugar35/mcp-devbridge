# Security

## v0.8.8 security model

MCP DevBridge intentionally grants a remote MCP client powerful local-development capabilities. v0.8.8 expands routing across multiple simultaneously-running roots, but that routing layer does not expand the filesystem boundary beyond roots the user explicitly started.

## Network boundary

- CodexPro, the OAuth/Bearer Gateway, optional Windows-MCP, and the legacy backend bind to loopback interfaces.
- Public traffic reaches the Gateway only through an explicitly configured Cloudflare/ngrok/Quick Tunnel.
- Public clients authenticate with OAuth or a compatible Bearer path before requests are proxied upstream.
- Loopback-anonymous behavior exists only for local compatibility paths; control/public paths continue to require authentication as defined by the component.
- Public URL and credential are separate concepts. Bearer tokens, OAuth secrets, tunnel tokens, and heartbeat secrets must never be embedded in the public MCP URL.

## Multi-root authorization and routing

Every READY project root is active at the same time. This is a routing model, not a permission bypass.

- Absolute paths are canonicalized before matching active roots.
- The most specific containing active root wins when roots are nested.
- Relative paths route automatically only when exactly one active root is supported by the evidence.
- Equally-valid matches are rejected as ambiguous and require an absolute path.
- A `task_id` is bound to the root/engine that created it.
- An opaque CodexPro `workspace_id` is only weak follow-up affinity and cannot override stronger path/cwd evidence.
- A historical session/workspace selection or bootstrap project must not silently override an explicit target path.
- One tool call that clearly spans multiple active roots must be split or explicitly disambiguated rather than silently executed in one root.

## Filesystem containment

Containment uses canonical/real-path semantics. Textual prefix checks are insufficient.

- `..` cannot escape the routed root.
- POSIX symlinks and Windows symlinks/junctions cannot make a textual in-root path resolve outside the root.
- Gateway-local `run_command` / `run_program` validates `cwd` through the same root boundary before spawning.
- Root-drive tree/inventory scans may encounter protected directories. `EACCES` / `EPERM` subtrees are skipped with warnings so scanning continues, but the permission error never grants access to those directories.
- Scoped Git discovery may walk upward only to identify the nearest repository relevant to the target path; it does not broaden arbitrary file access.

## Permission and execution profiles

The desktop combines file permissions with command profiles:

| Desktop mode | File permission | Command profile |
|---|---|---|
| Read-only | `read_only` | `safe` |
| Project workspace | `workspace` | `developer` |
| Full access (default, dangerous) | `system` | `full_system` |

New desktop projects intentionally default to full access. The first actual use still requires the one-time risk acknowledgement. Users can reduce permission to project-workspace or read-only mode.

`system/full_system` permits system-level work such as registry or environment configuration when requested. It does not disable hard blocks for known destructive disk/boot/system command patterns. Path restrictions on a tool’s working directory also remain separate from what an explicitly-authorized system command is allowed to do.

## Shell task security

Every public CodexPro `bash` invocation uses the same PathGuard, workspace selection, Bash session policy, execution profile, destructive-command checks, and environment sanitization before spawning.

`BashTaskManager` is process-scoped so a task can be found across MCP transport/session replacement, while task ownership remains workspace-scoped. Output is stored only in bounded in-memory rolling buffers and is redacted before MCP responses. `cancel_task` terminates the process tree rather than only the shell parent.

The 600-second orchestration watchdog is advisory. It marks a stale snapshot and supplies a resume hint; it never changes task status or sends a termination signal. Running tasks are intentionally not persisted across DevBridge/CodexPro restarts.

v0.8.8 transport liveness is also bounded. Gateway keepalives are comment-only SSE frames emitted only at complete event boundaries. CodexPro HTTP uses MCP TypeScript SDK v2 per-request stateless serving, so there is no protocol-session TTL or unbounded session transport registry. Cross-request application state is deliberately separated from transport state: workspace/task/long-run state is process scoped, while client-specific selected-workspace and last-shown checkpoints are keyed by a bounded client-affinity map. Gateway derives that affinity from a SHA-256 digest of existing session/auth context when necessary; the raw credential is never forwarded as the affinity and the affinity is not an authorization input. Request-tied progress notifications are emitted only when the client supplied the MCP progress token; DevBridge never invents one. Running `wait_task` transport snapshots use small redacted output tails, while full bounded output remains available through explicit task inspection.

## Durable long-run state security

The v0.8.8 long-run layer persists orchestration metadata, not arbitrary shell output. `.ai-bridge/long-runs/<run_id>.json` is guarded by the same canonical workspace PathGuard, rejects context-directory symlink/junction escape, uses bounded schemas and atomic replacement, and caps each run state file at 512 KiB. In-process writes to the same run are serialized to prevent concurrent checkpoints from silently overwriting each other.

Plan text, notes, evidence, rework items and completion summaries pass the same secret-looking-value detector before persistence. Raw Bearer/OAuth/tunnel credentials must never be used as long-run evidence; store a redacted description, exit code, artifact digest or external record reference instead.

`long_run_review` and `long_run_complete` fail closed on active background work. A task id that is missing after a CodexPro restart is `unknown`, not implicitly successful. The user/agent must persist explicit terminal evidence before resolving that task. Terminal failed/cancelled tasks remain visible to the evaluator; only a current PASS review can decide whether the overall acceptance criteria are nevertheless satisfied.

Native MCP Tasks may later provide a protocol-level handle when the client advertises support, but extension negotiation must never weaken these local persistence/path/secret/review gates.

## Secrets storage

### Windows

Protected values prefer Windows Credential Manager. The existing encrypted DPAPI file is retained as the fallback. Secrets are not plaintext fields in `projects.json`.

### Linux / SteamOS

Protected values prefer the desktop Secret Service when available. The fallback is an AES-GCM encrypted user-level store. New key/ciphertext files are created with user-only permissions where POSIX mode bits are available, and the application config directories are created with user-only access.

### Common rules

The following must never be written in plaintext to repository files, normal config JSON, audit logs, URLs, or upgrade-resume metadata:

- MCP Bearer tokens;
- OAuth client/access/refresh secrets;
- Cloudflare tunnel tokens;
- remote-device Bearer/heartbeat credentials;
- other values whose field names are recognized as secret-like.

Gateway comparisons for compatible Bearer authentication use timing-safe equality where applicable.

## OAuth Hub model

v0.8.8 OAuth authorizes the Hub. The browser consent page does not require selecting an “entry project”. After authorization, the concrete active root is selected from the actual tool call’s routing evidence and the Gateway swaps to that project’s upstream credential for the proxied request.

Legacy per-project Bearer behavior may remain for compatibility. It must not become a hidden routing fence for a normal Hub OAuth session.

## Multi-device security

Pairing codes are short-lived and memory-only. `devices.json` contains non-secret identity/endpoint/timestamp metadata only; per-device authentication and heartbeat secrets remain in `SecretsStore`.

Remote endpoints must be HTTPS except explicitly local development/test paths. The Hub uses a remote device credential only for the outbound proxy request and does not return that credential through MCP tools.

Device selection and active-root routing are separate boundaries: selecting a remote device does not expose its filesystem paths to local path resolution; routing is delegated to that device.

## Audit and logging

Gateway audit is on the real public `tools/call` path. Sensitive arguments such as command/content/patch values and recognized secret-like keys are redacted before persistence or display. Diagnostic and process logs must avoid printing credentials, tokens, or raw authorization headers.

## Installer and update security

Windows releases are per-user and support a user-selected install location. Detached upgrade metadata is non-secret; the restarted process reloads credentials from `SecretsStore`.

Linux/SteamOS installs are user-level. `install.sh` canonicalizes custom target directories and refuses destructive roots such as `/`, `$HOME`, and `$HOME/.local`, as well as unrelated non-empty targets. Desktop-entry command lines are escaped before being written.

Relative `XDG_CONFIG_HOME` / `XDG_DATA_HOME` values are treated as invalid rather than being used as attacker-controlled relative filesystem roots.

Release history from newer v0.9.x branches/tags must not be rewritten or force-pushed as part of the v0.8.8 maintenance release.


## Hub credential isolation

The client-facing Hub bearer is global to the shared Gateway. Project access values authenticate Gateway-to-CodexPro upstream hops only and are never inherited from or promoted into the Hub bearer. This prevents any project credential from becoming an implicit entry identity.
