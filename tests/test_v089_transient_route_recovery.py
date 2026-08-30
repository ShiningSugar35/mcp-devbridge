from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.routing_state import load_workspace_routes


@pytest.mark.asyncio
async def test_transient_project_unready_preserves_route_then_rehydrates_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "d-root"
    root.mkdir()
    project = ProjectConfig(
        id="d",
        display_name="d",
        root_path=str(root),
        codexpro_port=19000,
        permission_mode="system",
    )
    save_projects([project])

    state = {"ready": True, "configured": True, "hydrated": True}
    calls: list[str] = []

    def running_registry(project_id: str):
        if project_id == project.id and state["ready"]:
            return project.codexpro_port, project.root_path
        return None

    def configured_registry(project_id: str) -> str | None:
        if project_id == project.id and state["configured"]:
            return project.root_path
        return None

    def upstream(request: httpx.Request) -> httpx.Response:
        rpc = json.loads(request.content or b"{}")
        params = rpc.get("params") or {}
        name = str(params.get("name") or "")
        calls.append(name)
        if name == "open_workspace":
            state["hydrated"] = True
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "opened"}],
                        "structuredContent": {"workspace_id": "ws_transient"},
                    },
                },
            )
        if name in {"show_changes", "write"} and not state["hydrated"]:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "CodexProError: Unknown workspace_id: ws_transient. "
                                    "Call open_workspace first."
                                ),
                            }
                        ],
                        "structuredContent": {
                            "error": (
                                "CodexProError: Unknown workspace_id: ws_transient. "
                                "Call open_workspace first."
                            )
                        },
                    },
                },
            )
        if name == "show_changes":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "changes ok"}]
                    },
                },
            )
        if name == "write":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {"content": [{"type": "text", "text": "write ok"}]},
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=running_registry,
        workspace_project_registry=configured_registry,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=httpx.MockTransport(upstream),
    )
    gateway._remember_persistent_workspace_handle(
        "ws_transient", project.id, str(root)
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            state["ready"] = False
            unavailable = await client.post(
                "/mcp",
                headers={"accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "show_changes",
                        "arguments": {"workspace_id": "ws_transient"},
                    },
                },
            )
            assert unavailable.status_code == 502
            assert "尚未运行" in unavailable.text
            assert calls == []
            assert gateway._workspace_handle_roots["ws_transient"] == project.id
            assert gateway._workspace_route_records["ws_transient"]["root"] == str(
                root.resolve()
            )
            assert [record["handle"] for record in load_workspace_routes()] == [
                "ws_transient"
            ]

            state["ready"] = True
            state["hydrated"] = False
            recovered = await client.post(
                "/mcp",
                headers={"accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "show_changes",
                        "arguments": {"workspace_id": "ws_transient"},
                    },
                },
            )
            assert recovered.status_code == 200
            assert calls == ["show_changes", "open_workspace", "show_changes"]
            assert "changes ok" in recovered.text

            calls.clear()
            state["hydrated"] = False
            mutation = await client.post(
                "/mcp",
                headers={"accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "write",
                        "arguments": {
                            "workspace_id": "ws_transient",
                            "path": "x.txt",
                            "content": "x",
                        },
                    },
                },
            )
            assert mutation.status_code == 200
            assert calls == ["write", "open_workspace"]
            assert "原调用未自动重放" in mutation.text

            calls.clear()
            state["ready"] = False
            state["configured"] = False
            save_projects([])
            deleted = await client.post(
                "/mcp",
                headers={"accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "show_changes",
                        "arguments": {"workspace_id": "ws_transient"},
                    },
                },
            )
            assert deleted.status_code == 200
            assert "上下文已失效" in deleted.text
            assert calls == []
            assert "ws_transient" not in gateway._workspace_handle_roots
            assert "ws_transient" not in gateway._workspace_route_records
            assert load_workspace_routes() == []
    finally:
        gateway.stop()
