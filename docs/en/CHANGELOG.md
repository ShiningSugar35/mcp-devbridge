# Changelog

All dates 2026-08-06 … 2026-08-09 unless noted.

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
- Default port mismatch (`8765` vs `2865`) — unification pending.
- Real-device Smoke Test results apply to the verified Cloudflare setup
  (mcp.shiningsugar.shop); other zones require the same route steps.