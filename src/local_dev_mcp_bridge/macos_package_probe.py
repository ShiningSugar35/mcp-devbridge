"""Isolated, bounded verification of the MCP engine actually shipped in a Mac app."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .engines import CodexProManager


def _checked_result(tools: list[Any], call: Any) -> dict[str, Any]:
    names = {str(getattr(tool, "name", "")) for tool in tools}
    if not tools or "server_config" not in names:
        raise RuntimeError("Packaged MCP lacks its read-only canary")
    if bool(getattr(call, "is_error", getattr(call, "isError", False))):
        raise RuntimeError("Packaged MCP read-only canary returned an error")
    if not any(getattr(part, "text", "") for part in (getattr(call, "content", None) or [])):
        raise RuntimeError("Packaged MCP read-only canary returned no data")
    return {"mcp": "passed", "tool_count": len(tools), "read_only_call": "passed"}


async def _check_protocol(url: str, credential: str) -> dict[str, Any]:
    # Only the loopback URL constructed below is accepted; never a public server.
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or parsed.path != "/mcp" or parsed.query or parsed.fragment):
        raise ValueError("Expected the owned loopback endpoint")
    async with (
        asyncio.timeout(10),
        httpx.AsyncClient(
            headers={"Authorization": "Bearer " + credential}, timeout=3,
            follow_redirects=False, trust_env=False,
        ) as client,
        streamable_http_client(url=url, http_client=client) as streams,  # type: ignore[arg-type]
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        tools = list(listing.tools)
        if not any(tool.name == "server_config" for tool in tools):
            raise RuntimeError("Packaged MCP lacks its read-only canary")
        call = await session.call_tool("server_config", {})
        return _checked_result(tools, call)


def _wait_for_listener(engine: CodexProManager) -> None:
    deadline = time.monotonic() + 12
    with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            if not engine.is_running:
                raise RuntimeError("Packaged engine exited during startup")
            try:
                response = client.get(f"http://127.0.0.1:{engine.port}/healthz")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("Packaged engine listener deadline exceeded")


def run_bundled_engine_probe(
    bundle: Path, node: Path, dist: Path, profile: Path,
) -> dict[str, Any]:
    bundle, node, dist, profile = (path.resolve() for path in (bundle, node, dist, profile))
    if not node.is_relative_to(bundle) or not dist.is_relative_to(bundle):
        raise RuntimeError("Runtime is outside the owned app bundle")
    required = (node, dist / "http.js", dist.parent / "package.json")
    if not all(path.is_file() for path in required) or not (dist.parent / "node_modules").is_dir():
        raise RuntimeError("Required package input is missing")
    if profile.is_relative_to(bundle) or bundle.is_relative_to(profile):
        raise RuntimeError("An isolated profile outside the app is required")
    profile.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="engine-smoke-", dir=profile))
    engine: CodexProManager | None = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        credential = secrets.token_urlsafe(32)
        engine = CodexProManager(node_exe=str(node), dist_dir=dist, log_dir=workspace / "logs", port=port)
        engine.start(str(workspace), credential, permission_mode="read_only")
        _wait_for_listener(engine)
        return asyncio.run(_check_protocol(f"http://127.0.0.1:{port}/mcp", credential))
    except Exception:
        # Child logs are already scrubbed by EngineManager. Never echo protocol
        # bodies, credentials or arbitrary exception strings into CI receipts.
        raise RuntimeError("Packaged MCP engine probe failed") from None
    finally:
        if engine is not None:
            try:
                engine.stop(timeout_seconds=5)
            except Exception:
                raise RuntimeError("Packaged engine cleanup incomplete; workspace retained") from None
            if engine.is_running:
                raise RuntimeError("Packaged engine cleanup incomplete; workspace retained")
        shutil.rmtree(workspace)
