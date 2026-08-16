# Development

## Environment

```powershell
python -m venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev,package]"
```

Pyright is explicitly configured with `venvPath = "."` / `venv = ".venv"` so analysis uses the same environment as pytest.

## Verification

```powershell
$env:PYTHONIOENCODING="utf-8"
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pyright
```

v0.7.2 verification baseline: **304 passed** + full CodexPro smoke, Ruff clean, Pyright 0 errors / 0 warnings, plus a Qt offscreen smoke covering the six-column table, no-wheel combos, ChatGPT/Gemini visibility, per-project persistence and upgrade-resume consumption.

## Packaging while an older bridge is running

```powershell
.\scripts\build.ps1 -Version 0.7.2
```

The build no longer reuses `dist/MCPDevBridge`. It writes to `dist/staging-<version>/MCPDevBridge` and uses that directory as the Inno Setup source, so an older live executable cannot lock the new build. The installer is emitted as `release/MCPDevBridge-Setup-<version>.exe`; `cloudflared.exe` is copied beside the staging executable and the frozen runtime resolves that packaged copy first.

Before replacing a live bridge, use the detached updater so the process hosting the current MCP session can be replaced safely:

```powershell
.\scripts\live_upgrade.ps1 -InstallerPath .\release\MCPDevBridge-Setup-0.7.2.exe -ProjectRoot D:\path\to\project -OldPid <running-pid> -FallbackExe .\dist\staging-0.7.2\MCPDevBridge\MCPDevBridge.exe
```

The updater writes only non-secret `upgrade-resume.json` metadata, stops only the named old process tree, installs silently, replaces the desktop shortcut, launches the new executable, and waits for the selected project's loopback service to become ready. Use `-DryRun` first to verify the scheduled-task relay without stopping or installing anything.
