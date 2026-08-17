from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from local_dev_mcp_bridge.audit import AuditLogger, AuditQuery, query_logs
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.device_hub import PAIR_RECEIPT_TTL_SECONDS, DeviceRegistry
from local_dev_mcp_bridge.gateway import OAuthGateway, _inject_tools
from local_dev_mcp_bridge.models import ProjectConfig

HUB_TOKEN = "hub-access-value-123456789012345"
REMOTE_TOKEN = "remote-access-value-123456789012"


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _register_remote(registry: DeviceRegistry, *, device_id: str = "friend-pc", url: str = "https://friend.example/mcp") -> str:
    code, _expires = registry.generate_pair_code()
    return registry.register_remote(
        pair_code=code,
        device_id=device_id,
        name="朋友电脑",
        endpoint_url=url,
        bearer=REMOTE_TOKEN,
    )


def test_pairing_and_heartbeat_update_quick_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    store = MemoryStore()
    registry = DeviceRegistry(local_device_id="hub-pc", local_device_name="Hub", store=store, online_ttl_seconds=999)
    peer = _register_remote(registry, url="https://first-random.trycloudflare.com/mcp")
    target = registry.resolve_remote("friend-pc")
    assert target is not None
    assert target.base_url == "https://first-random.trycloudflare.com"

    registry.heartbeat(
        device_id="friend-pc",
        peer_secret=peer,
        endpoint_url="https://second-random.trycloudflare.com/mcp",
        name="朋友电脑",
        bearer=REMOTE_TOKEN,
    )
    target2 = registry.resolve_remote("friend-pc")
    assert target2 is not None
    assert target2.base_url == "https://second-random.trycloudflare.com"
    # devices.json stores metadata only; credentials stay in the provided store.
    raw = (tmp_path / "cfg" / "devices.json").read_text(encoding="utf-8")
    assert REMOTE_TOKEN not in raw
    assert peer not in raw


def test_gateway_pairing_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    store = MemoryStore()
    registry = DeviceRegistry(local_device_id="hub", local_device_name="Hub", store=store)
    code, _ = registry.generate_pair_code()
    gateway = OAuthGateway(
        public_hostname="hub.example.test",
        upstream_url="http://127.0.0.1:18787",
        upstream_legacy_token=lambda: HUB_TOKEN,
        device_registry=registry,
        local_device_id="hub",
    )
    client = TestClient(gateway.app, raise_server_exceptions=False)
    registered = client.post("/device/register", json={
        "pair_code": code,
        "device_id": "friend",
        "name": "Friend",
        "mcp_url": "https://old.trycloudflare.com/mcp",
        "bearer": REMOTE_TOKEN,
    })
    assert registered.status_code == 200, registered.text
    peer = registered.json()["peer_secret"]
    heartbeat = client.post("/device/heartbeat", json={
        "device_id": "friend",
        "peer_secret": peer,
        "name": "Friend",
        "mcp_url": "https://new.trycloudflare.com/mcp",
        "bearer": REMOTE_TOKEN,
    })
    assert heartbeat.status_code == 200
    assert registry.resolve_remote("friend").base_url == "https://new.trycloudflare.com"  # type: ignore[union-attr]


def test_single_online_remote_is_automatic_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    registry = DeviceRegistry(local_device_id="hub", local_device_name="Hub", store=MemoryStore(), online_ttl_seconds=999)
    _register_remote(registry)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("authorization", "")))
        return httpx.Response(200, json={"ok": True})

    gateway = OAuthGateway(
        public_hostname="hub.example.test",
        upstream_url="http://127.0.0.1:18787",
        upstream_legacy_token=lambda: HUB_TOKEN,
        workspace_registry=lambda _project_id: None,  # local computer is considered offline
        device_registry=registry,
        local_device_id="hub",
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(gateway.app, raise_server_exceptions=False)
    response = client.get("/mcp", headers={"Authorization": f"Bearer {HUB_TOKEN}", "mcp-session-id": "only-one"})
    assert response.status_code == 200, response.text
    assert calls[-1][0].startswith("https://friend.example/mcp")
    assert calls[-1][1] == f"Bearer {REMOTE_TOKEN}"
    assert "friend-pc" in gateway._get_current_device("only-one")


def test_device_switch_is_session_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    save_projects([ProjectConfig(id="local-project", display_name="Local", root_path=str(tmp_path))])
    registry = DeviceRegistry(local_device_id="hub", local_device_name="我的电脑", store=MemoryStore(), online_ttl_seconds=999)
    _register_remote(registry)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    gateway = OAuthGateway(
        public_hostname="hub.example.test",
        upstream_url="http://127.0.0.1:18787",
        upstream_legacy_token=lambda: HUB_TOKEN,
        workspace_registry=lambda pid: (18787, str(tmp_path)) if pid == "local-project" else None,
        workspace_credential_registry=lambda pid: HUB_TOKEN if pid == "local-project" else None,
        device_registry=registry,
        local_device_id="hub",
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(gateway.app, raise_server_exceptions=False)
    switch_rpc = {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"devbridge_switch_device","arguments":{"device_id":"friend-pc"}}}
    switched = client.post("/mcp", json=switch_rpc, headers={"Authorization":f"Bearer {HUB_TOKEN}","mcp-session-id":"session-a"})
    assert switched.status_code == 200

    client.get("/mcp", headers={"Authorization":f"Bearer {HUB_TOKEN}","mcp-session-id":"session-a"})
    assert calls[-1].startswith("https://friend.example/mcp")
    client.get("/mcp", headers={"Authorization":f"Bearer {HUB_TOKEN}","mcp-session-id":"session-b"})
    assert calls[-1].startswith("http://127.0.0.1:18787/mcp")


def test_injected_tools_are_deduplicated() -> None:
    payload = json.dumps({"jsonrpc":"2.0","id":1,"result":{"tools":[
        {"name":"devbridge_list_devices"}, {"name":"run_command"}
    ]}}).encode()
    rewritten = json.loads(_inject_tools(payload))
    names = [tool["name"] for tool in rewritten["result"]["tools"]]
    assert len(names) == len(set(names))
    assert names.count("devbridge_list_devices") == 1
    assert names.count("run_command") == 1


def test_gateway_writes_real_tool_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    audit_dir = tmp_path / "audit"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"ok"}]}})

    gateway = OAuthGateway(
        public_hostname="hub.example.test",
        upstream_url="http://127.0.0.1:18787",
        upstream_legacy_token=lambda: HUB_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    gateway._audit = AuditLogger(directory=audit_dir)
    client = TestClient(gateway.app, raise_server_exceptions=False)
    rpc = {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"}}}
    response = client.post("/mcp", json=rpc, headers={"Authorization":f"Bearer {HUB_TOKEN}"})
    assert response.status_code == 200
    rows = query_logs(AuditQuery(tool_name="read_file"), directory=audit_dir)
    assert len(rows) == 1
    assert rows[0]["success"] is True
    assert rows[0]["parameter_summary"]["path"] == "README.md"


def test_pair_registration_retry_is_idempotent(monkeypatch, tmp_path):
    store = MemoryStore()
    registry = DeviceRegistry(local_device_id="main-pc", local_device_name="Main", store=store)
    monkeypatch.setattr("local_dev_mcp_bridge.device_hub.load_devices", lambda: [])
    monkeypatch.setattr("local_dev_mcp_bridge.device_hub.upsert_device", lambda _device: None)
    code, _expires = registry.generate_pair_code()
    kwargs = dict(pair_code=code, device_id="friend-pc", name="Friend", endpoint_url="https://friend.example/mcp", bearer="b" * 32)
    first = registry.register_remote(**kwargs)
    second = registry.register_remote(**kwargs)
    assert second == first
    assert PAIR_RECEIPT_TTL_SECONDS == 1800
