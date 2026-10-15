"""macOS startup helpers. No configuration is rewritten and no process is stopped."""
from __future__ import annotations

import os
import platform
import plistlib
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path

BUNDLE_ID = "shop.shiningsugar.mcpdevbridge"
BUNDLE_NAME = "MCPDevBridge.app"
MINIMUM_MACOS = "14.4"
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?(?:\.post\d+)?\Z")


def architecture(machine: str | None = None) -> str:
    name = (platform.machine() if machine is None else machine).lower()
    aliases = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}
    try:
        return aliases[name]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported macOS architecture: {name}") from exc


def asset_name(version: str, machine: str | None = None) -> str:
    if not _VERSION.fullmatch(version):
        raise ValueError("Invalid release version")
    return f"MCPDevBridge-macOS-{architecture(machine)}-{version}.zip"


def native_config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "LocalDevMCPBridge"


def configuration_dir(env: MutableMapping[str, str], home: Path | None = None) -> Path:
    """Preserve an explicitly selected profile and existing pre-Mac port profiles."""
    home = home or Path.home()
    explicit = env.get("LOCALDEV_MCP_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    native = native_config_dir(home)
    names = ("config.json", "projects.json", "devices.json", "secrets.aesgcm", "secrets.key")
    if any((native / name).is_file() for name in names):
        return native
    legacy = [home / ".config" / "LocalDevMCPBridge"]
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():
        legacy.insert(0, Path(xdg) / "LocalDevMCPBridge")
    for path in legacy:
        if any((path / name).is_file() for name in names):
            return path
    return native


def bundle_for_executable(executable: Path) -> Path | None:
    """Find only this product's actual .app, not a partial path name."""
    executable = executable.resolve()
    if executable.name != "MCPDevBridge" or executable.parent.name != "MacOS":
        return None
    contents = executable.parent.parent
    bundle = contents.parent
    if contents.name != "Contents" or bundle.suffix != ".app":
        return None
    try:
        info = contents / "Info.plist"
        if info.stat().st_size > 65536:
            return None
        payload = plistlib.loads(info.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(payload, dict):
        return None
    return bundle if payload.get("CFBundleIdentifier") == BUNDLE_ID else None


def runtime_directories(executable: Path, meipass: Path | None = None) -> list[Path]:
    candidates = [meipass / "runtime"] if meipass else []
    bundle = bundle_for_executable(executable)
    if bundle:
        candidates.extend(bundle / "Contents" / area / "runtime" for area in ("Frameworks", "Resources", "MacOS"))
    return list(dict.fromkeys(candidates))


def prepare_environment(
    *, env: MutableMapping[str, str] | None = None,
    home: Path | None = None, system: str | None = None,
    executable: Path | None = None, meipass: Path | None = None,
) -> None:
    """Call before importing constants, Qt, or the desktop application."""
    if (system or sys.platform) != "darwin":
        return
    env = os.environ if env is None else env
    if not env.get("LOCALDEV_MCP_CONFIG_DIR", "").strip():
        env["LOCALDEV_MCP_CONFIG_DIR"] = str(configuration_dir(env, home))
    executable = executable or Path(sys.executable)
    if meipass is None and getattr(sys, "frozen", False):
        raw = getattr(sys, "_MEIPASS", None)
        meipass = Path(raw) if raw else None
    directories = runtime_directories(executable, meipass)
    for directory in directories:
        node = directory / "node"
        if node.is_file():
            env.setdefault("MCPDEVBRIDGE_NODE_EXE", str(node))
            break
    # Finder's PATH may omit command-line runtimes. Add only the owned .app's
    # private runtime directory, not a guessed Homebrew or unrelated directory.
    private = [str(directory) for directory in directories if (directory / "cloudflared").is_file()]
    if private:
        existing = env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep)
        private_set = set(private)
        env["PATH"] = os.pathsep.join([*private, *(entry for entry in existing if entry not in private_set)])


def self_test() -> int:
    """Isolated frozen package check, not a production Gemini linking test."""
    import json
    import shutil
    import subprocess

    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        raise RuntimeError("A native frozen macOS executable is required")
    if os.environ.get("MCPDEVBRIDGE_ISOLATED_SELFTEST") != "1":
        raise RuntimeError("An explicit isolated package-check environment is required")
    profile_value = os.environ.get("LOCALDEV_MCP_CONFIG_DIR", "")
    report_value = os.environ.get("MCPDEVBRIDGE_SELFTEST_REPORT", "")
    if not profile_value or not report_value or not Path(profile_value).is_absolute():
        raise RuntimeError("Explicit profile and report paths are required")
    profile = Path(profile_value).resolve()
    report_path = Path(report_value).resolve()
    bundle = bundle_for_executable(Path(sys.executable))
    if bundle is None or not report_path.is_relative_to(profile.parent) or report_path.exists():
        raise RuntimeError("Invalid package-check bundle or output path")
    from PySide6.QtWidgets import QApplication, QWidget

    from .engines import find_node

    node = find_node()
    tunnel = shutil.which("cloudflared")
    if not node or not tunnel:
        raise RuntimeError("Private runtimes are missing")
    required = architecture()
    for executable in (node, tunnel):
        slices = subprocess.check_output(["/usr/bin/lipo", "-archs", executable], text=True, timeout=10).split()
        if required not in slices:
            raise RuntimeError("Packaged runtime architecture mismatch")
        if not subprocess.check_output([executable, "--version"], text=True, timeout=10).strip():
            raise RuntimeError("Packaged runtime cannot execute")
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = QWidget()
    window.setWindowTitle("MCP DevBridge package check")
    window.show()
    app.processEvents()
    window.close()
    from .macos_package_probe import run_bundled_engine_probe

    runtime_root = Path(getattr(sys, "_MEIPASS", ""))
    result = {
        "platform": "macos", "architecture": required, "qt": "passed", "private_runtimes": "passed",
        **run_bundled_engine_probe(bundle, Path(node), runtime_root / "third_party" / "codexpro" / "dist", profile),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)
    if sys.stdout is not None:
        print(json.dumps(result))
    return 0


def main() -> int:
    prepare_environment()
    if "--macos-self-test" in sys.argv:
        return self_test()
    from .desktop_main import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
