"""GitHub Release discovery and platform-specific update handoff."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .platform_support import IS_LINUX, IS_WINDOWS, platform_key, popen_platform_kwargs

RELEASE_API = "https://api.github.com/repos/ShiningSugar35/mcp-devbridge/releases/latest"
WINDOWS_INSTALLER_PREFIX = "MCPDevBridge-Setup-"
LINUX_PACKAGE_PREFIX = "MCPDevBridge-Linux-x86_64-"
# Backward-compatible public name used by older callers/tests.
INSTALLER_PREFIX = WINDOWS_INSTALLER_PREFIX


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    download_url: str
    size: int
    sha256: str
    asset_name: str = ""
    platform: str = ""


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", value or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def is_newer(latest: str, current: str) -> bool:
    left = version_tuple(latest)
    right = version_tuple(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _release_asset_prefix() -> str:
    if IS_WINDOWS:
        return WINDOWS_INSTALLER_PREFIX
    if IS_LINUX:
        machine = platform.machine().lower()
        if machine not in {"x86_64", "amd64"}:
            raise RuntimeError(
                f"当前 Linux 架构 {machine or 'unknown'} 暂无 MCP DevBridge 桌面发布包；"
                "SteamOS/Steam Machine x86_64 已支持。"
            )
        return LINUX_PACKAGE_PREFIX
    raise RuntimeError(f"当前平台 {platform_key()} 暂不支持应用内升级。")


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
    prefix = _release_asset_prefix()
    asset = next(
        (item for item in assets if str(item.get("name") or "").startswith(prefix)),
        None,
    )
    if not asset:
        platform_name = "Windows" if IS_WINDOWS else "Linux/SteamOS"
        raise RuntimeError(f"最新 Release 没有 {platform_name} 安装包。")
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
        asset_name=str(asset.get("name") or ""),
        platform=platform_key(),
    )


def download_installer(info: ReleaseInfo, *, target_dir: Path | None = None) -> Path:
    """Download the current platform's installer/package and verify size/digest."""
    directory = target_dir or (Path(tempfile.gettempdir()) / "MCPDevBridge-Updates")
    directory.mkdir(parents=True, exist_ok=True)
    if info.asset_name:
        filename = info.asset_name
    elif IS_WINDOWS:
        filename = f"MCPDevBridge-Setup-{info.version}.exe"
    elif IS_LINUX:
        filename = f"MCPDevBridge-Linux-x86_64-{info.version}.tar.gz"
    else:
        raise RuntimeError(f"当前平台 {platform_key()} 暂不支持应用内升级。")
    target = directory / filename
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
    script_name = "live_upgrade.ps1" if IS_WINDOWS else "live_upgrade.sh"
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "scripts" / script_name
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[2] / "scripts" / script_name


def launch_update(installer: Path, *, project_root: str = "") -> None:
    script = bundled_upgrade_script()
    if not script.is_file():
        raise RuntimeError("找不到内置升级脚本。")
    if IS_WINDOWS:
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
    elif IS_LINUX:
        bash = shutil.which("bash") or "/bin/bash"
        args = [
            bash,
            str(script),
            "--package",
            str(installer),
            "--old-pid",
            str(os.getpid()),
        ]
        if project_root:
            args += ["--project-root", project_root]
    else:
        raise RuntimeError(f"当前平台 {platform_key()} 暂不支持应用内升级。")
    subprocess.Popen(
        args,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_platform_kwargs(detached=True),
    )  # noqa: S603


__all__ = [
    "ReleaseInfo",
    "fetch_latest_release",
    "is_newer",
    "download_installer",
    "launch_update",
    "bundled_upgrade_script",
    "INSTALLER_PREFIX",
    "WINDOWS_INSTALLER_PREFIX",
    "LINUX_PACKAGE_PREFIX",
]
