# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: MCP DevBridge desktop (onedir).

Build:  pyinstaller packaging/local-dev-mcp-bridge.spec --noconfirm
Output: dist/MCPDevBridge/
"""
from pathlib import Path

ROOT = Path(SPECPATH).parent
PROJECT_VERSION = "0.7.1"

a = Analysis(
    [str(ROOT / "packaging" / "entry_desktop.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
datas=[
        (str(ROOT / "third_party" / "codexpro" / "dist"), "third_party/codexpro/dist"),
        (str(ROOT / "third_party" / "codexpro" / "node_modules"), "third_party/codexpro/node_modules"),
        (str(ROOT / "THIRD_PARTY_LICENSES.md"), "THIRD_PARTY_LICENSES.md"),
    ],
    hiddenimports=[
        "local_dev_mcp_bridge.models",
        "local_dev_mcp_bridge.config_store",
        "local_dev_mcp_bridge.secrets",
        "local_dev_mcp_bridge.project_secrets",
        "local_dev_mcp_bridge.device_hub",
        "local_dev_mcp_bridge.help_content",
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

# Bundle the cloudflared binary next to the executable.
from shutil import copy2

TOOLS = ROOT / ".tools"
if (TOOLS / "cloudflared.exe").is_file():
    copy2(TOOLS / "cloudflared.exe", str(Path(DISTPATH) / "MCPDevBridge" / "cloudflared.exe"))
