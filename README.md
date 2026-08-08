# MCP DevBridge

A Windows desktop app that exposes a **local development project** as a
Model Context Protocol (MCP) server over a **stable HTTPS URL** backed by a
Cloudflare Named Tunnel — ready for ChatGPT / Gemini / Claude and other MCP
clients (formerly named **LocalDev MCP Bridge**).

## Feature summary

- Pick a local project, click **Start**, get a **fixed public MCP URL**
  (`https://mcp.<your-domain>/mcp`) that never changes across restarts.
- Public MCP identity presented as `mcp-devbridge` (initialize `serverInfo`),
  no matter which engine binary sits behind the gateway.
- Permission modes: read-only / project workspace / full system access.
- 33 MCP tools: file read/write/search, command execution (PowerShell),
  git status/commit/push, process management, env discovery and more.
- Public access requires a Bearer token (generated and stored via Windows
  Credential Manager / DPAPI); loopback anonymous access is optional.
- Engine stack (CodexPro-compatible Node server), optional Windows control
  bridge, tunneling (`cloudflared` named/quick or `ngrok` fixed), audit log
  with secret redaction, log viewer tabs, Git desktop settings.

## Quick start

```powershell
cd D:\Environment\mcp\local-dev-mcp-bridge
python -m venv .venv
.venv\Scripts\activate
uv pip install -e ".[dev,package]"

# run the desktop app
.venv\Scripts\python.exe -m local_dev_mcp_bridge.desktop_main

# or headless backend for one workspace
.venv\Scripts\python.exe -m local_dev_mcp_bridge.standalone_server <workspace> --port 8765
```

## Documentation (Chinese)

- 开发计划.md — development plan and acceptance criteria
- 项目架构.md — architecture and security model
- 进度验收.md — progress and acceptance log
- AGENTS.md — contributor guide

## English docs

- [ARCHITECTURE.md](docs/en/ARCHITECTURE.md)
- [SECURITY.md](docs/en/SECURITY.md)
- [COMPATIBILITY.md](docs/en/COMPATIBILITY.md)
- [DEVELOPMENT.md](docs/en/DEVELOPMENT.md)
- [CHANGELOG.md](docs/en/CHANGELOG.md)