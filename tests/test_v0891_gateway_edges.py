"""Protocol regressions use placeholders only; no real credentials or network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import local_dev_mcp_bridge.gateway as gm
from local_dev_mcp_bridge.gateway import OAuthGateway

ACCESS_CODE = "[REDACTED_SECRET]"


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(gm, "_LOG_DIR", tmp_path / "logs")


def call(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "edge-91",
        "method": "tools/call",
        "params": {"name": "read", "arguments": arguments or {}},
    }


def gateway(handler: Any, **kwargs: Any) -> OAuthGateway:
    return OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_url="http://upstream.test",
        upstream_legacy_token=lambda: ACCESS_CODE,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def post(gw: OAuthGateway, payload: Any, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(
        app=gw.app, client=("127.0.0.1", 1234), raise_app_exceptions=False
    )
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/mcp", params={"token": ACCESS_CODE}, json=payload, **kwargs)
    finally:
        await gw._http.aclose()
        gw.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["", "Bearer", "Basic wrong", "bearer\t"])
async def test_present_malformed_header_never_falls_back(authorization: str) -> None:
    attempts = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "edge-91", "result": {}})

    response = await post(gateway(upstream), call(), headers={"authorization": authorization})
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}
    assert attempts == 0


@pytest.mark.asyncio
async def test_duplicate_authorization_is_rejected() -> None:
    gw = gateway(lambda _req: httpx.Response(200, json={"result": {}}))
    response = await post(
        gw,
        call(),
        headers=[("authorization", "Bearer " + ACCESS_CODE), ("authorization", "Bearer wrong")],
    )
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [[], "bad", 42])
async def test_bad_params_returns_correlated_protocol_error(value: Any) -> None:
    attempts = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"result": {}})

    payload = call()
    payload["params"] = value
    response = await post(gateway(upstream), payload)
    assert response.status_code == 400
    assert response.json()["id"] == "edge-91"
    assert response.json()["error"]["code"] == -32602
    assert attempts == 0


@pytest.mark.asyncio
async def test_offline_explicit_root_preserves_request_id() -> None:
    gw = gateway(lambda _req: httpx.Response(200), workspace_registry=lambda _id: None)
    response = await post(gw, call({"devbridge_workspace_id": "offline-root"}))
    assert response.status_code == 502
    assert response.json()["id"] == "edge-91"


@pytest.mark.asyncio
async def test_unreachable_mutation_is_not_replayed_and_has_protocol_error() -> None:
    attempts = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("fixture disconnect", request=request)

    payload = call()
    payload["params"]["name"] = "write"
    response = await post(gateway(upstream), payload)
    assert response.status_code == 502
    assert response.json()["jsonrpc"] == "2.0"
    assert response.json()["id"] == "edge-91"
    assert attempts == 1


class Stream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.payload
        if self.fail:
            raise httpx.ReadError("fixture body disconnect")

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["initialize", "tools/list"])
async def test_tool_text_cannot_select_control_plane(
    keyword: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rewritten: list[str] = []

    def spy(payload: bytes) -> bytes:
        rewritten.append(keyword)
        return payload

    monkeypatch.setattr(gm, "_rewrite_server_identity", spy)
    monkeypatch.setattr(gm, "_inject_tools", spy)
    stream = Stream(b'data: {"jsonrpc":"2.0","id":"edge-91","result":{"content":[]}}\n\n')
    response = await post(
        gateway(
            lambda _req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        call({"text": keyword}),
    )
    assert response.status_code == 200
    assert rewritten == []
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_control_sse_buffer_has_same_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "_UPSTREAM_BUFFER_MAX_BYTES", 64)
    stream = Stream(b"data: " + b"x" * 128 + b"\n\n")
    response = await post(
        gateway(
            lambda _req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        {"jsonrpc": "2.0", "id": "edge-91", "method": "initialize", "params": {}},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == -32009
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_buffered_body_disconnect_is_correlated_and_closed() -> None:
    stream = Stream(b'{"jsonrpc":"2.0",', fail=True)
    response = await post(
        gateway(
            lambda _req: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
        call(),
    )
    assert response.status_code == 502
    assert response.json()["id"] == "edge-91"
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_body_limit_without_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "_MCP_REQUEST_MAX_BYTES", 64, raising=False)
    gw = gateway(lambda _req: httpx.Response(200, json={"result": {}}))

    async def chunks() -> AsyncIterator[bytes]:
        yield b" " * 40
        yield b" " * 40
        yield json.dumps(call()).encode()

    transport = httpx.ASGITransport(app=gw.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/mcp", params={"token": ACCESS_CODE}, content=chunks())
        assert response.status_code == 413
    finally:
        await gw._http.aclose()
        gw.stop()


@pytest.mark.asyncio
async def test_body_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "_MCP_REQUEST_READ_SECONDS", 0.01, raising=False)
    gw = gateway(lambda _req: httpx.Response(200, json={"result": {}}))

    async def chunks() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.05)
        yield json.dumps(call()).encode()

    transport = httpx.ASGITransport(app=gw.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/mcp", params={"token": ACCESS_CODE}, content=chunks())
        assert response.status_code == 408
    finally:
        await gw._http.aclose()
        gw.stop()


@pytest.mark.asyncio
async def test_tool_catalog_does_not_rehash_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = gm._tools_response_summary

    def count(payload: bytes) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(gm, "_tools_response_summary", count)
    response = await post(
        gateway(lambda _req: httpx.Response(500)),
        {"jsonrpc": "2.0", "id": "edge-91", "method": "tools/list"},
    )
    assert response.status_code == 200
    assert len(response.json()["result"]["tools"]) == 50
    assert calls == 0


def test_diagnostic_plain_text_redacts_capability() -> None:
    secret = "[REDACTED_SECRET]"
    value = gm._diag_redact_body("network error https://mcp.example.test/mcp?key=" + secret)
    assert secret not in value


def test_diagnostic_log_rotates_at_bounded_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "_DIAG_MAX_BYTES", 512, raising=False)
    for number in range(20):
        gm._write_diag_entry(event="rotation_probe", number=number, note="x" * 120)
    files = list(gm._diag_log_path().parent.glob("gateway-*.jsonl*"))
    assert len(files) == 2
    assert all(path.stat().st_size <= 512 for path in files)
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line), dict)


def test_diagnostic_nested_fields_redact_url() -> None:
    gm._write_diag_entry(
        event="privacy_probe",
        details={"nested": ["https://mcp.example.test/mcp?key=" + ACCESS_CODE]},
    )
    assert ACCESS_CODE not in gm._diag_log_path().read_text(encoding="utf-8")


def test_diagnostic_retention_only_removes_owned_old_logs() -> None:
    import os
    import time

    directory = gm._diag_log_path().parent
    old = directory / "gateway-2000-01-01.jsonl.1"
    unrelated = directory / "user-notes.log"
    for path in (old, unrelated):
        path.write_text("sentinel", encoding="utf-8")
        then = time.time() - 8 * 86_400
        os.utime(path, (then, then))
    gm._write_diag_entry(event="retention_probe")
    assert not old.exists()
    assert unrelated.read_text(encoding="utf-8") == "sentinel"


def test_diagnostic_concurrent_writers_keep_valid_complete_records() -> None:
    from concurrent.futures import ThreadPoolExecutor

    def emit(number: int) -> None:
        gm._write_diag_entry(event="concurrent_probe", number=number)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(emit, range(160)))
    entries = [
        json.loads(line) for line in gm._diag_log_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 160
    assert {entry["number"] for entry in entries} == set(range(160))
