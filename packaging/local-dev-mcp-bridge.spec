# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller spec for MCP DevBridge desktop (onedir).

Windows output: dist/.../MCPDevBridge/MCPDevBridge.exe
Linux output:   dist/.../MCPDevBridge/MCPDevBridge
"""
from pathlib import Path
from shutil import copy2
import sys

ROOT = Path(SPECPATH).parent
PROJECT_VERSION = "0.9.4"
IS_WINDOWS = sys.platform == "win32"
TOOLS = ROOT / ".tools"
RUNTIME = TOOLS if IS_WINDOWS else TOOLS / "linux"
CODEXPRO_RUNTIME = ROOT / "build" / "codexpro-runtime"
if not (CODEXPRO_RUNTIME / "dist").is_dir() or not (CODEXPRO_RUNTIME / "node_modules").is_dir():
    raise RuntimeError("Missing build/codexpro-runtime; run scripts/prepare_codexpro_runtime.py first")

runtime_datas = []
if IS_WINDOWS:
    for name in ("node.exe", "uv.exe", "uvx.exe"):
        candidate = RUNTIME / name
        if candidate.is_file():
            runtime_datas.append((str(candidate), "runtime"))
    upgrade_script = ROOT / "scripts" / "live_upgrade.ps1"
else:
    candidate = RUNTIME / "node"
    if candidate.is_file():
        runtime_datas.append((str(candidate), "runtime"))
    upgrade_script = ROOT / "scripts" / "live_upgrade.sh"

a = Analysis(
    [str(ROOT / "packaging" / "entry_desktop.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(CODEXPRO_RUNTIME / "dist"), "third_party/codexpro/dist"),
        (str(CODEXPRO_RUNTIME / "node_modules"), "third_party/codexpro/node_modules"),
        (str(ROOT / "THIRD_PARTY_LICENSES.md"), "THIRD_PARTY_LICENSES.md"),
        (str(upgrade_script), "scripts"),
        *runtime_datas,
    ],
    hiddenimports=[
        "local_dev_mcp_bridge.models",
        "local_dev_mcp_bridge.config_store",
        "local_dev_mcp_bridge.secrets",
        "local_dev_mcp_bridge.project_secrets",
        "local_dev_mcp_bridge.device_hub",
        "local_dev_mcp_bridge.help_content",
        "local_dev_mcp_bridge.update_manager",
        "local_dev_mcp_bridge.platform_support",
        "local_dev_mcp_bridge.agent_pool",
        "local_dev_mcp_bridge.chatgpt_desktop",
        "local_dev_mcp_bridge.agent_orchestrator",
        "local_dev_mcp_bridge.audit",
        "local_dev_mcp_bridge.shell",
        "local_dev_mcp_bridge.processes",
        "local_dev_mcp_bridge.permissions",
        "local_dev_mcp_bridge.tools",
        "local_dev_mcp_bridge.server_factory",
        "local_dev_mcp_bridge.server_main",
        "local_dev_mcp_bridge.standalone_server",
        "local_dev_mcp_bridge.selftest",
        "local_dev_mcp_bridge.engines",
        "local_dev_mcp_bridge.tunnel_manager",
        "local_dev_mcp_bridge.app_state",
        "local_dev_mcp_bridge.backend_manager",
        "local_dev_mcp_bridge.oauth_provider",
        "local_dev_mcp_bridge.gateway",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "mcp.server",
        "platformdirs",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MCPDevBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MCPDevBridge",
)

# The tunnel manager intentionally looks next to the desktop executable. Keep
# cloudflared there on both platforms; this also makes manual diagnostics easy.
cloudflared_name = "cloudflared.exe" if IS_WINDOWS else "cloudflared"
cloudflared = RUNTIME / cloudflared_name
if cloudflared.is_file():
    target = Path(DISTPATH) / "MCPDevBridge" / cloudflared_name
    copy2(cloudflared, target)
    if not IS_WINDOWS:
        target.chmod(0o755)
