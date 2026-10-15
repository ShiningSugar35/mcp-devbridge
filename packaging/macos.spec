# -*- mode: python ; coding: utf-8 -*-
"""Build on the target Mac architecture; Windows/Linux spec remains unchanged."""
from pathlib import Path
import os
import platform
import sys
import tomllib

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if sys.platform != "darwin":
    raise RuntimeError("The .app must be built on macOS")
ROOT = Path(SPECPATH).parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
ARCH = platform.machine().lower()
if ARCH not in {"arm64", "x86_64"}:
    raise RuntimeError("Unsupported native build architecture")
RUNTIME = ROOT / ".tools" / ("macos-" + ARCH)
CODEX = ROOT / "build" / "codexpro-runtime"
for required in (RUNTIME / "node", RUNTIME / "cloudflared", CODEX / "dist", CODEX / "node_modules"):
    if not required.exists():
        raise RuntimeError("Missing Mac build input: " + str(required))

analysis = Analysis(
    [str(ROOT / "packaging" / "entry_macos.py")],
    pathex=[str(ROOT / "src")],
    binaries=[(str(RUNTIME / name), "runtime") for name in ("node", "cloudflared")],
    datas=[
        (str(CODEX / "dist"), "third_party/codexpro/dist"),
        (str(CODEX / "package.json"), "third_party/codexpro"),
        (str(CODEX / "node_modules"), "third_party/codexpro/node_modules"),
        (str(ROOT / "THIRD_PARTY_LICENSES.md"), "THIRD_PARTY_LICENSES.md"),
    ] + collect_data_files("mcp"),
    hiddenimports=collect_submodules("local_dev_mcp_bridge") + [
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="MCPDevBridge", console=False, strip=False, upx=False,
    target_arch=ARCH, argv_emulation=False,
    codesign_identity=os.environ.get("MACOS_CODESIGN_IDENTITY") or None,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name="MCPDevBridge", strip=False, upx=False)
bundle = BUNDLE(
    collection, name="MCPDevBridge.app", bundle_identifier="shop.shiningsugar.mcpdevbridge",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "MCP DevBridge",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "14.4",
        "NSHighResolutionCapable": True,
    },
)
