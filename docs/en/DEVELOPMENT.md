# Development

## Environment

```powershell
python -m venv .venv
.venv\Scripts\activate
uv pip install -e ".[dev,package]"
```

## Commands

```powershell
# tests (run at the project root)
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe -m pytest tests/ -q

# lint
.venv\Scripts\python.exe -m ruff check src tests

# run the desktop
.venv\Scripts\python.exe -m local_dev_mcp_bridge.desktop_main
```

## Conventions

- Do **not** modify external projects or directories other than this repo
  (test scaffolding lives in `.test-workspace`).
- Never commit secrets; tokens only via SecretsStore; logs are redacted.
- Chinese UI strings and messages; doc updates are part of every phase
  (进度验收.md first, then code).
- New tests must pass before a phase is marked complete. Current suite:
  154 tests.

## Adding an MCP tool

1. Implement in `tools.py` following existing decorators/patterns.
2. `mcp_integration` tests exercise tool registration + permission gates.
3. Update docs (Chinese & English COMPATIBILITY) and the test count in docs.

## Packaging

```powershell
.\scripts\build.ps1              # pytest + ruff + PyInstaller onedir
ISCC scripts\installer.iss       # Inno Setup installer → release\
```

## Verification checklist (smoke)

1. Start the desktop, add a project, generate token.
2. `LOCAL` mode → self-test should pass (`127.0.0.1:<port>/mcp`).
3. Cloudflare mode with token → status "已连接"; `curl https://host/mcp`
   gets 401 without Bearer, 200/SSE with Bearer.