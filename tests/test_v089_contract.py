from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import (
    OAuthGateway,
    _stable_tools_list_payload,
    _tools_response_summary,
)
from local_dev_mcp_bridge.hub_tool_contract import (
    HUB_TOOL_CONTRACT_FINGERPRINT,
    HUB_TOOL_CONTRACT_VERSION,
    HUB_TOOL_COUNT,
)
from local_dev_mcp_bridge.models import PermissionMode, ProjectConfig


def _assert_stable_contract(payload: bytes) -> None:
    summary = _tools_response_summary(payload)
    assert summary == {
        "outcome": "tools_result",
        "count": HUB_TOOL_COUNT,
        "duplicates": [],
        "schema_fingerprint": HUB_TOOL_CONTRACT_FINGERPRINT,
        "error_code": None,
    }


def test_embedded_contract_payload_matches_frozen_release_identity() -> None:
    assert HUB_TOOL_CONTRACT_VERSION == 1
    assert HUB_TOOL_COUNT == 50
    _assert_stable_contract(_stable_tools_list_payload("release-contract"))


@pytest.mark.parametrize("permission_mode", ["read_only", "workspace", "system"])
@pytest.mark.parametrize("project_running", [False, True])
@pytest.mark.parametrize("upstream_kind", ["json", "sse", "error", "partial"])
@pytest.mark.asyncio
async def test_public_contract_is_invariant_across_runtime_and_upstream_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    permission_mode: PermissionMode,
    project_running: bool,
    upstream_kind: str,
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "repo"
    root.mkdir()
    project = ProjectConfig(
        id="contract-project",
        display_name="contract-project",
        root_path=str(root),
        codexpro_port=19189,
        permission_mode=permission_mode,
    )
    save_projects([project])
    upstream_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        rpc = json.loads(request.content or b"{}")
        if upstream_kind == "json":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "tools": [
                            {
                                "name": "runtime_only_tool",
                                "description": "must never become public",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        if upstream_kind == "sse":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":'
                    b'[{"name":"runtime_sse_tool","inputSchema":{"type":"object"}}]}}\n\n'
                ),
            )
        if upstream_kind == "error":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "error": {"code": -32000, "message": "runtime error"},
                },
            )
        return httpx.Response(200, content=b'{"jsonrpc":"2.0","result":')

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://bootstrap-a.test",
        allow_local_anonymous=True,
        workspace_registry=(
            lambda project_id: (project.codexpro_port, project.root_path)
            if project_running and project_id == project.id
            else None
        ),
        workspace_credential_registry=lambda _project_id: "project-token",
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            for index, accept in enumerate(
                ("application/json", "application/json, text/event-stream"), start=1
            ):
                gateway.upstream_url = f"http://bootstrap-{index}.test"
                response = await client.post(
                    "/mcp",
                    headers={"accept": accept},
                    json={
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("application/json")
                _assert_stable_contract(response.content)
    finally:
        gateway.stop()

    assert upstream_calls == 0


@pytest.mark.asyncio
async def test_gateway_health_exposes_nonsecret_contract_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.get("/health")
    finally:
        gateway.stop()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["hub_tool_contract_version"] == HUB_TOOL_CONTRACT_VERSION
    assert body["hub_tool_count"] == HUB_TOOL_COUNT
    assert body["hub_tool_schema_fingerprint"] == HUB_TOOL_CONTRACT_FINGERPRINT
    assert "token" not in response.text.lower()
    assert "authorization" not in response.text.lower()
