from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

import local_dev_mcp_bridge.gateway as gateway_module
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig


class OneShotStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.iterations = 0
        self.closed = 0

    async def __aiter__(self):
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("upstream stream was consumed more than once")
        yield self.payload

    async def aclose(self) -> None:
        self.closed += 1


class DelayedSseStream(httpx.AsyncByteStream):
    def __init__(self, delay_seconds: float, payload: bytes) -> None:
        self.delay_seconds = delay_seconds
        self.payload = payload
        self.iterations = 0
        self.closed = 0
        self.cancelled = 0

    async def __aiter__(self):
        self.iterations += 1
        try:
            await asyncio.sleep(self.delay_seconds)
            yield self.payload
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def aclose(self) -> None:
        self.closed += 1


def _tool_call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 91,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


@pytest.mark.asyncio
async def test_ordinary_tool_call_has_total_deadline_before_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        gateway_module, "_UPSTREAM_ORDINARY_DEADLINE_SECONDS", 0.025, raising=False
    )
    attempts = 0
    cancelled = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, cancelled
        attempts += 1
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        rpc = json.loads(request.content or b"{}")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": rpc.get("id"), "result": {"content": []}},
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call("read", {"path": "README.md"}),
        )
    elapsed = time.monotonic() - started
    gateway.stop()

    assert elapsed < 0.10
    assert response.status_code == 504
    assert response.json()["error"]["code"] == -32008
    assert attempts == 1
    assert cancelled == 1


@pytest.mark.asyncio
async def test_ordinary_sse_body_deadline_closes_upstream_and_emits_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        gateway_module, "_UPSTREAM_ORDINARY_DEADLINE_SECONDS", 0.030, raising=False
    )
    stream = DelayedSseStream(
        0.20,
        b'data: {"jsonrpc":"2.0","id":91,"result":{"content":[]}}\n\n',
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call("read", {"path": "README.md"}),
        )
    elapsed = time.monotonic() - started
    gateway.stop()

    assert elapsed < 0.12
    assert response.status_code == 200
    assert b'"code":-32008' in response.content.replace(b" ", b"")
    assert stream.iterations == 1
    assert stream.cancelled == 1
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_sse_audit_records_terminal_timeout_instead_of_header_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        gateway_module, "_UPSTREAM_ORDINARY_DEADLINE_SECONDS", 0.025, raising=False
    )
    stream = DelayedSseStream(
        0.20,
        b'data: {"jsonrpc":"2.0","id":91,"result":{"content":[]}}\n\n',
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    audits: list[tuple[bool, str]] = []

    def audit_spy(
        _request, _rpc, _tool_name, _workspace_id, _device_id, success: bool,
        *, duration_ms: int, error_type: str = ""
    ) -> None:
        del duration_ms
        audits.append((success, error_type))

    monkeypatch.setattr(gateway, "_audit_gateway_tool", audit_spy)
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call("read", {"path": "README.md"}),
        )
    gateway.stop()

    assert response.status_code == 200
    assert audits == [(False, "deadline_exceeded")]


@pytest.mark.asyncio
async def test_chunked_json_body_deadline_returns_structured_504_before_downstream_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        gateway_module, "_UPSTREAM_ORDINARY_DEADLINE_SECONDS", 0.030, raising=False
    )
    stream = DelayedSseStream(
        0.20,
        b'{"jsonrpc":"2.0","id":91,"result":{"content":[]}}',
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call("read", {"path": "README.md"}),
        )
    elapsed = time.monotonic() - started
    gateway.stop()

    assert elapsed < 0.12
    assert response.status_code == 504
    assert response.json()["error"]["code"] == -32008
    assert stream.cancelled == 1
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_chunked_json_body_has_hard_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(gateway_module, "_UPSTREAM_BUFFER_MAX_BYTES", 64, raising=False)
    stream = DelayedSseStream(
        0.0,
        b'{"jsonrpc":"2.0","id":91,"result":{"content":"'
        + (b"x" * 256)
        + b'"}}',
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call("show_changes", {"include_diff": True}),
        )
    gateway.stop()

    assert response.status_code == 502
    assert response.json()["error"]["code"] == -32009
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_open_workspace_sse_body_uses_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        gateway_module, "_UPSTREAM_ORDINARY_DEADLINE_SECONDS", 0.030, raising=False
    )
    stream = DelayedSseStream(
        0.20,
        b'data: {"jsonrpc":"2.0","id":91,"result":{"content":[]}}\n\n',
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    save_projects(
        [
            ProjectConfig(
                id="p",
                display_name="p",
                root_path=str(tmp_path),
                codexpro_port=19000,
                permission_mode="system",
            )
        ]
    )

    def registry(project_id: str):
        return (19000, str(tmp_path)) if project_id == "p" else None

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=registry,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json=_tool_call(
                "open_workspace",
                {"root": str(tmp_path), "devbridge_workspace_id": "p"},
            ),
        )
    elapsed = time.monotonic() - started
    gateway.stop()

    assert elapsed < 0.12
    assert response.status_code == 504
    assert response.json()["error"]["code"] == -32008
    assert stream.cancelled == 1
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_initialize_reuses_buffered_one_shot_json_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    stream = OneShotStream(
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25",'
        b'"capabilities":{},"serverInfo":{"name":"codexpro","version":"1"}}}'
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(tmp_path),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "deadline-test", "version": "1"},
                },
            },
        )
    gateway.stop()

    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "mcp-devbridge"
    assert stream.iterations == 1
    assert stream.closed == 1
