# Architecture

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
| `tunnel_manager.py` | Cloudflare Named, ngrok reserved domain, Quick Tunnel and Local modes. Quick/ngrok/fixed public URLs normalize to `/mcp`. |
| `models.py` | `ProjectConfig`, including permission, client target, connection, per-project ports, Git and Gemini redirect URI. |

## Multi-project state

`projects.json` stores non-sensitive project settings. Sensitive values never enter that JSON: project Bearers and Cloudflare tokens are stored through Windows Credential Manager or the DPAPI fallback. One project can own the full public entry while other project engines remain live; the Gateway routes MCP sessions to the requested running workspace.

The desktop polls state every second. Entry-project state comes from `ServiceCoordinator`; other project rows come from their `ProjectUnit`, preventing a connected project from remaining visually stuck at “未启动”.

## Upgrade handoff

Builds use versioned `dist/staging-<version>` directories so a running older executable never blocks PyInstaller cleanup. A detached updater may write `%LOCALAPPDATA%\LocalDevMCPBridge\upgrade-resume.json` with non-secret project metadata. The new desktop consumes it after startup, reloads credentials from SecretsStore and restores the service when prior risk acknowledgement allows unattended startup.
