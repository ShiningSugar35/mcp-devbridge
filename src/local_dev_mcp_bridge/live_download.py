"""Optional public download for the separately built portable client."""

from __future__ import annotations

import hashlib
import html
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route


def install_download(app: Starlette, path: Path) -> None:
    archive = path.resolve(strict=True)
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise ValueError("MCP Live 下载文件必须是有效的 ZIP。")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    asset_url = f"/download/artifacts/{digest}.zip"
    size_mb = archive.stat().st_size / 1_000_000
    filename = html.escape(archive.name)
    page = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Live 下载</title>
<style>
body {{font: 16px/1.6 system-ui,sans-serif; margin: 0; color:#18283b; background:#f4f7fb}}
main {{width:min(34rem, calc(100% - 3rem)); margin:12vh auto; background:white;
  box-sizing:border-box; padding:clamp(18px,5vw,36px); border:1px solid #dae3ed; border-radius:12px}}
h1 {{margin:0; font-size:2rem}} p {{margin:12px 0 20px}}
a {{display:inline-block; padding:9px 17px; color:white; background:#245bd7; border-radius:7px}}
code {{display:block; font-size:0.82rem; overflow-wrap:anywhere}}
small {{display:block; margin-top:18px; color:#52627a}}
</style>
<main>
<h1>MCP Live</h1>
<p>Windows x64 便携端 · {size_mb:.1f} MB</p>
<a href="{asset_url}">下载 ZIP</a>
<small>{filename}</small>
<small>SHA-256</small>
<code>{digest}</code>
</main>
</html>"""

    async def download_page(_: Request) -> HTMLResponse:
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            },
        )

    async def latest_archive(_: Request) -> RedirectResponse:
        return RedirectResponse(asset_url, headers={"Cache-Control": "no-store"})

    async def download_archive(_: Request) -> FileResponse:
        return FileResponse(
            archive, filename=archive.name, media_type="application/zip",
            headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{digest}"'},
        )

    app.routes.extend([
        Route("/download", download_page, methods=["GET"]),
        Route("/download/windows", latest_archive, methods=["GET"]),
        Route(asset_url, download_archive, methods=["GET"]),
    ])
