"""Prepare the pinned Windows-control runtime when a Live user enables it."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import httpx
from platformdirs import user_cache_path

from .engines import BUNDLED_UV_VERSION, find_uvx

_UV_ARCHIVE_NAME = "uv-x86_64-pc-windows-msvc.zip"
_UV_ARCHIVE_URL = f"https://github.com/astral-sh/uv/releases/download/{BUNDLED_UV_VERSION}/{_UV_ARCHIVE_NAME}"
_UV_ARCHIVE_SIZE = 24_889_479
_UV_ARCHIVE_SHA256 = "15bfd1423b7eaa7aae949922d4712ebaac2bb44a81af64ab59bbe007090cb0d0"
_EXECUTABLES = ("uv.exe", "uvx.exe")


def _available(directory: Path) -> bool:
    return all((directory / filename).is_file() for filename in _EXECUTABLES)


async def windows_uvx() -> str:
    installed = find_uvx()
    if installed:
        return installed

    parent = user_cache_path("MCP Live", appauthor=False) / "runtimes"
    destination = parent / f"uv-{BUNDLED_UV_VERSION}-{_UV_ARCHIVE_SHA256[:12]}"
    uvx = destination / "uvx.exe"
    if _available(destination):
        return str(uvx)

    try:
        parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="uv-", dir=parent) as stage:
            staging = Path(stage)
            archive = staging / _UV_ARCHIVE_NAME
            checksum = hashlib.sha256()
            size = 0
            async with (
                httpx.AsyncClient(follow_redirects=True, timeout=30) as client,
                client.stream("GET", _UV_ARCHIVE_URL) as response,
            ):
                response.raise_for_status()
                with archive.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > _UV_ARCHIVE_SIZE:
                            raise RuntimeError("Windows 控制组件大小校验失败。")
                        checksum.update(chunk)
                        output.write(chunk)
            if size != _UV_ARCHIVE_SIZE or checksum.hexdigest() != _UV_ARCHIVE_SHA256:
                raise RuntimeError("Windows 控制组件完整性校验失败。")

            payload = staging / "runtime"
            payload.mkdir()
            with ZipFile(archive) as package:
                for filename in _EXECUTABLES:
                    member, = (item for item in package.infolist() if Path(item.filename).name == filename)
                    with package.open(member) as original, (payload / filename).open("wb") as output:
                        shutil.copyfileobj(original, output)
            try:
                payload.rename(destination)
            except OSError:
                if not _available(destination):
                    raise
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError("无法准备 Windows 控制组件，请检查网络后重试。") from exc

    return str(uvx)
