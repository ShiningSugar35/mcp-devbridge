"""Small cross-platform helpers for Windows and Linux desktop builds.

MCP DevBridge started as a Windows-first application.  SteamOS Desktop Mode is
an Arch-Linux desktop, so platform differences must stay explicit instead of
being scattered as ``if sys.platform`` checks across the UI and process layer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


def platform_key() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_LINUX:
        return "linux"
    if IS_MACOS:
        return "macos"
    return sys.platform or "unknown"


def runtime_filename(base: str) -> str:
    """Return the packaged executable filename for this OS."""
    return f"{base}.exe" if IS_WINDOWS else base


def popen_platform_kwargs(*, new_session: bool = False, detached: bool = False) -> dict[str, Any]:
    """Arguments safe to pass to ``subprocess.Popen`` on the active OS.

    Windows uses creation flags while POSIX uses process sessions/groups.  Passing
    Windows flags on Linux raises ``ValueError: creationflags is only supported on
    Windows``; this helper prevents that entire class of cross-platform failure.
    """
    if IS_WINDOWS:
        flags = 0
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if new_session:
            flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if detached:
            flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        return {"creationflags": flags}
    return {"start_new_session": bool(new_session or detached)}


def run_platform_kwargs() -> dict[str, Any]:
    """Cross-platform kwargs for short ``subprocess.run`` probes."""
    if IS_WINDOWS:
        return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0))}
    return {}


def open_in_file_manager(path: Path) -> bool:
    """Open a directory/file with the desktop's normal file manager."""
    target = str(path.expanduser().resolve())
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        if IS_MACOS:
            subprocess.Popen(["open", target], **popen_platform_kwargs(detached=True))
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen(
                [opener, target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_platform_kwargs(detached=True),
            )
            return True
    except OSError:
        return False
    return False


def desktop_dir() -> Path:
    """Best-effort per-user desktop directory without requiring root access."""
    if IS_WINDOWS:
        return Path.home() / "Desktop"
    # KDE/SteamOS respects XDG user dirs.  Avoid invoking shell code just to find
    # it; parse the generated config if present and otherwise use ~/Desktop.
    config_root = _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")

    config = config_root / "user-dirs.dirs"
    if config.is_file():
        try:
            for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("XDG_DESKTOP_DIR="):
                    continue
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                return Path(value).expanduser()
        except OSError:
            pass
    return Path.home() / "Desktop"


def linux_install_root() -> Path:
    """User-writable install location suitable for SteamOS' immutable base OS."""
    override = os.environ.get("MCPDEVBRIDGE_INSTALL_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".local" / "opt" / "MCPDevBridge"


def _xdg_home(name: str, fallback: Path) -> Path:
    """Return an XDG base directory, ignoring invalid relative overrides."""
    raw = os.environ.get(name, "").strip()
    candidate = Path(raw).expanduser() if raw else None
    return candidate if candidate is not None and candidate.is_absolute() else fallback


def linux_desktop_entry_path() -> Path:
    data_home = _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return data_home / "applications" / "mcp-devbridge.desktop"


def linux_autostart_entry_path() -> Path:
    config_home = _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
    return config_home / "autostart" / "mcp-devbridge.desktop"


__all__ = [
    "IS_WINDOWS",
    "IS_LINUX",
    "IS_MACOS",
    "platform_key",
    "runtime_filename",
    "popen_platform_kwargs",
    "run_platform_kwargs",
    "open_in_file_manager",
    "desktop_dir",
    "linux_install_root",
    "linux_desktop_entry_path",
    "linux_autostart_entry_path",
]
