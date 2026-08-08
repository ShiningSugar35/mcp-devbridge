"""End-to-end MCP integration tests over a real backend subprocess."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from local_dev_mcp_bridge.models import RuntimeConfig
from local_dev_mcp_bridge.secrets import SecretsStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Backend:
    """Spawn the real backend CLI subprocess and manage its lifecycle."""

    def __init__(self, rc: RuntimeConfig, config_path: Path, port: int) -> None:
        self.rc = rc
        self.config_path = config_path
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "local_dev_mcp_bridge.server_main",
            "--config",
            str(self.config_path),
            "--port",
            str(self.port),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                output = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"backend exited early (rc={self.proc.returncode}):\n{output}")
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("backend did not become healthy in time")

    def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(Exception):
            self.proc.terminate()
            self.proc.wait(timeout=5)
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> _Backend:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


@pytest.fixture()
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real backend subprocess isolated to a temp config dir and workspace."""
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALDEV_MCP_NO_CREDENTIAL_MANAGER", "1")
    workspace_dir = tmp_path / "工作区 空间"
    workspace_dir.mkdir(exist_ok=True)
    config_path = tmp_path / "runtime.json"

    def _start(**overrides) -> _Backend:
        rc = RuntimeConfig(
            workspace=str(workspace_dir),
            log_dir=str(tmp_path / "logs"),
            **overrides,
        )
        config_path.write_text(json.dumps(rc.model_dump(), ensure_ascii=False), encoding="utf-8")
        return _Backend(rc, config_path, _free_port())

    return _start


async def _mcp_call(session: ClientSession, name: str, arguments: dict | None = None) -> dict:
    result = await session.call_tool(name, arguments or {})
    return {
        "is_error": getattr(result, "isError", False) or getattr(result, "is_error", False),
        "text": "".join(getattr(c, "text", "") for c in result.content),
    }


@pytest.mark.asyncio
class TestMCPIntegration:
    async def test_initialize_and_tool_count(self, backend):
        with backend() as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    info = await session.initialize()
                    assert info.server_info.name
                    tools = await session.list_tools()
                    assert len(tools.tools) >= 30

    async def test_workspace_file_roundtrip(self, backend):
        with backend() as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await _mcp_call(session, "write_file", {"path": "a.txt", "content": "hello mcp\n"})
                    assert not res["is_error"], res
                    res = await _mcp_call(session, "read_file", {"path": "a.txt"})
                    assert "hello mcp" in res["text"]
                    res = await _mcp_call(session, "list_directory", {"path": "."})
                    assert "a.txt" in res["text"]

    async def test_path_escape_rejected(self, backend):
        with backend() as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await _mcp_call(session, "read_file", {"path": "../outside.txt"})
                    assert res["is_error"]
                    assert "项目根目录之外" in res["text"]

    async def test_unknown_tool(self, backend):
        with backend() as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await session.call_tool("no_such_tool_xyz", {})
                    assert getattr(res, "is_error", False) or getattr(res, "isError", False)

    async def test_write_blocked_in_read_only_mode(self, backend):
        with backend(permission_mode="read_only") as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await _mcp_call(session, "write_file", {"path": "x.txt", "content": "nope"})
                    assert res["is_error"]

    async def test_project_info(self, backend):
        with backend() as srv:
            async with streamable_http_client(srv.base_url + "/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await _mcp_call(session, "get_workspace_info")
                    assert not res["is_error"]
                    assert "工作区 空间" in res["text"]


@pytest.mark.asyncio
class TestAuthAndRateLimit:
    async def test_health_open(self, backend):
        import httpx

        with backend() as srv:
            async with httpx.AsyncClient() as client:
                r = await client.get(srv.base_url + "/health")
                assert r.status_code == 200

    async def test_public_requires_bearer(self, backend):
        import httpx

        with backend() as srv:
            store = SecretsStore(use_credential_manager=False)
            token = store.get("LocalDevMCPBridge/AccessToken")
            assert token, "backend should have created an access token"
            async with httpx.AsyncClient() as client:
                headers = {"cf-connecting-ip": "8.8.8.8"}
                r = await client.post(
                    srv.base_url + "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    headers=headers,
                )
                assert r.status_code == 401
                r = await client.get(srv.base_url + "/health", headers=headers)
                assert r.status_code == 200
                r = await client.post(
                    srv.base_url + "/mcp",
                    json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
                    headers={**headers, "Authorization": f"Bearer {token}"},
                )
                assert r.status_code in (200, 202)

    async def test_rate_limit_lockout(self, backend):
        import httpx

        with backend() as srv:
            async with httpx.AsyncClient() as client:
                headers = {"cf-connecting-ip": "8.8.8.8"}
                statuses = []
                for _ in range(12):
                    r = await client.post(
                        srv.base_url + "/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        headers=headers,
                    )
                    statuses.append(r.status_code)
                assert statuses.count(401) >= 1
                assert statuses[-1] == 429
