"""GitHub Release update discovery and installer download for the desktop app."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

RELEASE_API = "https://api.github.com/repos/ShiningSugar35/mcp-devbridge/releases/latest"
INSTALLER_PREFIX = "MCPDevBridge-Setup-"


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    download_url: str
    size: int
    sha256: str


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", value or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def is_newer(latest: str, current: str) -> bool:
    left = version_tuple(latest)
    right = version_tuple(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def fetch_latest_release(*, timeout: float = 10.0) -> ReleaseInfo:
    response = httpx.get(
        RELEASE_API,
        timeout=timeout,
        follow_redirects=True,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MCP-DevBridge"},
    )
    response.raise_for_status()
    payload = response.json()
    assets = payload.get("assets") or []
    asset = next(
        (item for item in assets if str(item.get("name") or "").startswith(INSTALLER_PREFIX)),
        None,
    )
    if not asset:
        raise RuntimeError("最新 Release 没有 Windows 安装包。")
    digest = str(asset.get("digest") or "")
    sha256 = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
    return ReleaseInfo(
        version=str(payload.get("tag_name") or "").lstrip("v"),
        tag=str(payload.get("tag_name") or ""),
        name=str(payload.get("name") or payload.get("tag_name") or "新版"),
        notes=str(payload.get("body") or "").strip(),
        download_url=str(asset.get("browser_download_url") or ""),
        size=int(asset.get("size") or 0),
        sha256=sha256,
    )


def download_installer(info: ReleaseInfo, *, target_dir: Path | None = None) -> Path:
    directory = target_dir or (Path(tempfile.gettempdir()) / "MCPDevBridge-Updates")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"MCPDevBridge-Setup-{info.version}.exe"
    digest = hashlib.sha256()
    size = 0
    with httpx.stream(
        "GET",
        info.download_url,
        timeout=httpx.Timeout(300.0, connect=30.0),
        follow_redirects=True,
        headers={"User-Agent": "MCP-DevBridge"},
    ) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    if info.size and size != info.size:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"安装包大小校验失败：期望 {info.size}，实际 {size}。")
    if info.sha256 and digest.hexdigest().lower() != info.sha256:
        target.unlink(missing_ok=True)
        raise RuntimeError("安装包 SHA-256 校验失败，已拒绝安装。")
    return target


def bundled_upgrade_script() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "scripts" / "live_upgrade.ps1"
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[2] / "scripts" / "live_upgrade.ps1"


def launch_update(installer: Path, *, project_root: str = "") -> None:
    script = bundled_upgrade_script()
    if not script.is_file():
        raise RuntimeError("找不到内置升级脚本。")
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-InstallerPath",
        str(installer),
        "-OldPid",
        str(os.getpid()),
    ]
    if project_root:
        args += ["-ProjectRoot", project_root]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, creationflags=flags, close_fds=True)  # noqa: S603


__all__ = [
    "ReleaseInfo",
    "fetch_latest_release",
    "is_newer",
    "download_installer",
    "launch_update",
    "bundled_upgrade_script",
]
