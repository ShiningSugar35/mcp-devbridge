from __future__ import annotations

import json
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.device_hub import DeviceRegistry
from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS, OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig

HUB_CRED = chr(72) * 40
REMOTE_CRED = chr(82) * 40


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _registry() -> DeviceRegistry:
    store = MemoryStore()
    registry = DeviceRegistry(
        local_device_id="hub-pc",
        local_device_name="Hub",
        store=store,
        online_ttl_seconds=999,
    )
    code, _ = registry.generate_pair_code()
    registry.register_remote(
        pair_code=code,
        device_id="friend-pc",
        name="Friend",
        endpoint_url="https://friend.example/mcp",
        bearer=REMOTE_CRED,
    )
    return registry


def _gateway(tmp_path: Path, handler) -> OAuthGateway:
    save_projects(
        [
            ProjectConfig(
                id="local-project",
                display_name="Local",
                root_path=str(tmp_path),
                codexpro_port=18787,
            )
        ]
    )
    return OAuthGateway(
        public_hostname="hub.example.test",
        workspace=str(tmp_path),
        upstream_url="http://127.0.0.1:18787",
        upstream_legacy_token=lambda: HUB_CRED,
        workspace_registry=lambda pid: (18787, str(tmp_path)) if pid == "local-project" else None,
        workspace_credential_registry=lambda pid: HUB_CRED if pid == "local-project" else None,
        device_registry=_registry(),
        local_device_id="hub-pc",
        transport=httpx.MockTransport(handler),
    )


def test_workspace_management_tools_expose_formal_device_id() -> None:
    tools = {str(tool["name"]): tool for tool in _PYTHON_TOOL_DEFS}
    for name in (
        "devbridge_list_workspaces",
        "devbridge_get_current_workspace",
        "devbridge_switch_workspace",
    ):
        properties = tools[name]["inputSchema"]["properties"]
        assert "device_id" in properties
    assert tools["devbridge_switch_workspace"]["inputSchema"]["required"] == ["project_id"]
    for name in (
        "agent_pool_capabilities",
        "agent_pool_spawn",
        "agent_pool_spawn_batch",
        "agent_pool_list",
        "agent_pool_get",
        "agent_pool_wait",
        "agent_pool_cancel",
        "agent_pool_collect",
        "agent_pool_cleanup",
    ):
        assert "device_id" in tools[name]["inputSchema"]["properties"]
    assert "project_id" in tools["agent_pool_spawn"]["inputSchema"]["properties"]
    assert "project_id" in tools["agent_pool_spawn_batch"]["inputSchema"]["properties"]
    assert "claude" in tools["agent_pool_spawn"]["inputSchema"]["properties"]["executor"]["enum"]


def test_explicit_remote_device_workspace_query_is_stateless(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((str(request.url), request.headers.get("authorization", ""), payload))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": "REMOTE_WORKSPACES"}]},
            },
        )

    gateway = _gateway(tmp_path, handler)
    client = TestClient(gateway.app, raise_server_exceptions=False)
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "devbridge_list_workspaces",
            "arguments": {"device_id": "friend-pc"},
        },
    }
    response = client.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {HUB_CRED}"})
    assert response.status_code == 200, response.text
    assert calls[-1][0].startswith("https://friend.example/mcp")
    assert calls[-1][1] == f"Bearer {REMOTE_CRED}"
    assert calls[-1][2]["params"]["arguments"]["device_id"] == "friend-pc"


def test_explicit_device_route_survives_transport_session_recreation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload.get("id"), "result": {"content": []}},
        )

    gateway = _gateway(tmp_path, handler)
    client = TestClient(gateway.app, raise_server_exceptions=False)
    for sid, rpc_id in (("transport-a", 1), ("transport-b", 2)):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {
                    "name": "devbridge_list_workspaces",
                    "arguments": {"device_id": "friend-pc"},
                },
            },
            headers={"Authorization": f"Bearer {HUB_CRED}", "mcp-session-id": sid},
        )
        assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert all(url.startswith("https://friend.example/mcp") for url in calls)
    assert "transport-a" not in gateway._session_devices
    assert "transport-b" not in gateway._session_devices


def test_explicit_remote_device_workspace_switch_proxies_to_remote(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        seen["payload"] = payload
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": "REMOTE_SWITCH_OK"}]},
            },
        )

    gateway = _gateway(tmp_path, handler)
    client = TestClient(gateway.app, raise_server_exceptions=False)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "devbridge_switch_workspace",
                "arguments": {"device_id": "friend-pc", "project_id": "remote-project"},
            },
        },
        headers={"Authorization": f"Bearer {HUB_CRED}"},
    )
    assert response.status_code == 200, response.text
    assert str(seen["url"]).startswith("https://friend.example/mcp")
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["params"]["arguments"]["project_id"] == "remote-project"



def test_explicit_remote_agent_pool_spawn_routes_device_and_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        seen["payload"] = payload
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": "REMOTE_AGENT_QUEUED"}]},
            },
        )

    gateway = _gateway(tmp_path, handler)
    client = TestClient(gateway.app, raise_server_exceptions=False)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "agent_pool_spawn",
                "arguments": {
                    "device_id": "friend-pc",
                    "project_id": "remote-project",
                    "prompt": "inspect remote project",
                    "write": False,
                },
            },
        },
        headers={"Authorization": f"Bearer {HUB_CRED}"},
    )
    assert response.status_code == 200, response.text
    assert str(seen["url"]).startswith("https://friend.example/mcp")
    payload = seen["payload"]
    assert isinstance(payload, dict)
    arguments = payload["params"]["arguments"]
    assert arguments["device_id"] == "friend-pc"
    assert arguments["project_id"] == "remote-project"
    assert arguments["prompt"] == "inspect remote project"



def test_formal_agent_project_route_does_not_change_session_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload.get("id"), "result": {}})

    gateway = _gateway(tmp_path, handler)

    class _FakePool:
        def spawn(self, **kwargs):
            return {"id": "fake-agent", "workspace": str(kwargs["workspace"]), "state": "queued"}

    gateway._agent_pool = _FakePool()  # type: ignore[assignment]
    client = TestClient(gateway.app, raise_server_exceptions=False)
    sid = "formal-project-session"
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "agent_pool_spawn",
                "arguments": {
                    "project_id": "local-project",
                    "prompt": "one-shot local project route",
                    "write": False,
                },
            },
        },
        headers={"Authorization": f"Bearer {HUB_CRED}", "mcp-session-id": sid},
    )
    assert response.status_code == 200, response.text
    assert sid not in gateway._session_workspaces
