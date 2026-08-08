# CodexPro fork notes (LocalDev MCP Bridge)

This directory is a **controlled fork** of [rebel0789/codexpro](https://github.com/rebel0789/codexpro),
kept inside the LocalDev MCP Bridge project (`third_party/codexpro`).

## Upstream provenance

| Item | Value |
|---|---|
| Upstream repo | `https://github.com/rebel0789/codexpro` |
| Pinned commit | `9b2a0ba7e4ccddced1ab88acb6e0d5092648a2b6` |
| Commit subject | Merge pull request #80 "agent/pr-maintenance-sweep" |
| Commit date | 2026-07-29 |
| Cloned (forked) on | 2026-08-08 |
| Version | `0.29.0` (`v0.29.0-beta.1-31-g9b2a0ba`) |
| License | MIT (see `LICENSE` at repo root) |

The upstream git history is preserved in this directory (`.git`). `node_modules` is a build
artifact only and must never be modified by hand (`npm ci` recreates it from `package-lock.json`).

## Local modifications (fork delta)

All changes are additive; no upstream behavior was removed.

1. **New module `src/windowsBridge.ts`** — adds three **always-registered** MCP tools
   (they are not gated by `toolMode`/`bashMode`/`writeMode` and never dynamically added/removed):

   - `windows_backend_status` (read-only): reports whether the local Windows-MCP bridge
     is reachable on its **fixed internal port** (default `127.0.0.1:28731/mcp`,
     override `CODEXPRO_WINDOWS_BRIDGE_URL`), plus server name/version and cached tool
     inventory. Never prints bearer tokens or public URLs.
   - `windows_list_tools` (read-only): inspects the bridge with `tools/list` and returns
     names + short descriptions.
   - `windows_call` (annotated `readOnlyHint=false`, `destructiveHint=true`): forwards one
     tool call to the bridge using the MCP `Client` + `StreamableHTTPClientTransport`
     from `@modelcontextprotocol/sdk`. Bearer token comes only from the process
     environment (`CODEXPRO_WINDOWS_BRIDGE_TOKEN`, min 24 bytes).

   Config env vars for the fork (all optional):
   - `CODEXPRO_WINDOWS_BRIDGE_URL` — bridge base URL; default `http://127.0.0.1:28731/mcp`
   - `CODEXPRO_WINDOWS_BRIDGE_TOKEN` — Bearer token sent to the bridge; never logged
   - `CODEXPRO_WINDOWS_CALL_TIMEOUT_MS` — per-call timeout; default `120000`

2. **`src/server.ts`** — imports `registerWindowsBridgeTools` and invokes it at the end of
   `createCodexProServer`, so the three tools are present on both the stdio and HTTP
   transports.

## Build & verify

```bash
npm ci          # recreate node_modules from lockfile (never commit it)
npm run build   # tsc -> dist/
```

Verify the tools appear (smoke-tested 2026-08-08, 23 tools in standard mode):

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

via `node dist/stdio.js --root <existing-dir>` and confirm
`windows_backend_status`, `windows_list_tools`, `windows_call` in the result.

## Constraints honored

- Upstream `LICENSE` retained; no license change.
- No `node_modules` modifications; no global `npm` installs; no `npm@latest`.
- No new model APIs or external network calls from the fork itself.
- The fork never exposes the Windows bridge publicly; the bridge itself only binds
  `127.0.0.1`.
