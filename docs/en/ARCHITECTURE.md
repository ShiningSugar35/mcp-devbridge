# Architecture

## Components

| Module | Responsibility |
|---|---|
| `desktop_main.py` | PySide6 single-window UI: project list, permission/connection config, Git settings tab, control buttons, token/URL box, self-test, three log tabs (control messages / process logs / audit logs). |
| `app_state.py` | `ServiceCoordinator` state machine — ordered start/stop (Codex engine → Windows bridge → tunnel), fixed-URL handling, failure cleanup, Qt-free. |
| `engines.py` | `CodexProManager` (Node engine, fixed local port **8787**, bound to 127.0.0.1), `WindowsBridgeManager`, `EngineManager` base (spawn/log/stop). |
| `tunnel_manager.py` | Tunnel process: Cloudflare Named Tunnel (`tunnel run --token ...`), Quick Tunnel, ngrok fixed domain, local-only; ready detection + URL parsing. |
| `server_factory.py` | Python MCP backend: `build_backend()` → `MCPServer` + Starlette app; bearer auth, rate limiting, audit middleware, transport security. |
| `server_main.py` / `standalone_server.py` | CLI entrypoints for the Python backend (nginx-less alternative stack). |
| `audit.py` | JSONL audit log, redaction, retention (14 days / 50 MB rotation), query API. |
| `models.py` | Pydantic `ProjectConfig` / `AppConfig` / `RuntimeConfig`; git field validation helpers. |
| `secrets.py` | Bearer tokens via Windows Credential Manager with DPAPI fallback. |

## Data flow

```
desktop_main ── StartOptions ──> ServiceCoordinator
                                   ├─ CodexProManager   (node http server :8787)
                                   ├─ WindowsBridgeManager (uvx, optional)
                                   └─ TunnelManager       (cloudflared/ngrok)
                                          └─ public URL https://mcp.<domain>/mcp
                                     Cloudflare edge ──HTTPS──> client (GPT/Gemini)
```

## Transport security (DNS rebinding)

The Python `streamable_http_app()` enables DNS-rebinding protection by
default and only allows loopback `Host` headers. Phase 4 solution (option B):

```python
build_transport_security(public_hostname)  # server_factory.py
# protection stays ON; allowed_hosts = loopback + exact hostname + "hostname:*"
```

`RuntimeConfig.public_hostname` is injected from the config file or
`standalone_server --public-hostname`. The desktop tunnel path terminates at
the Node engine (127.0.0.1:8787) which binds loopback only.

## Persistence

- `%LOCALAPPDATA%\LocalDevMCPBridge\config.json` — app config
- `...\projects.json` — project list
- `...\runtime.json` — session `RuntimeConfig`
- `...\process_logs\tunnel.log` — tunnel/engine tails
- audit logs in the config dir `logs\mcp-YYYY-MM-DD.jsonl`