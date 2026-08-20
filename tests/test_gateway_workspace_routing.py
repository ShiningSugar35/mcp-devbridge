"""Gateway workspace routing persistence.

ChatGPT recreates the MCP transport between tool batches (fresh
``mcp-session-id`` per batch), so session-bound state alone keeps losing the
workspace. The gateway must:

* persist the client's workspace selection (explicit route arg,
  ``devbridge_switch_workspace``, ``open_workspace``) across transports;
* replay ``open_workspace(root)`` on every fresh upstream session so engine
  tools that omit ``workspace_id`` keep resolving against the same root;
* rewrite ``open_current_workspace`` (which resets the engine to its default
  root) into an ``open_workspace`` of the recorded root.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig

PUB_TOKEN = "chatgpt-legacy-token-abc"
PROJ_A_ID = "aaaa1111"
PROJ_B_ID = "bbbb2222"


class FakeUpstream:
    """In-memory CodexPro engine: stateful initialize, records every request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        sid = request.headers.get("mcp-session-id", "")
        try:
            body = json.loads(request.content or b"{}")
        except Exception:
            body = {}
        method = str(body.get("method", ""))
        params = body.get("params") or {}
        name = str(params.get("name", "")) if isinstance(params, dict) else ""
        self.calls.append(
            {
                "port": request.url.port,
                "sid": sid,
                "method": method,
                "name": name,
                "arguments": params.get("arguments") if isinstance(params, dict) else None,
            }
        )
        if method == "initialize":
            if not sid:
                self.counter += 1
                sid = f"engine-{self.counter}"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "CodexPro", "version": "0.29.0"},
                    },
                },
                headers={"mcp-session-id": sid},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"content": [{"type": "text", "text": f"ok:{name}"}]},
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})


def _mcp_json(method: str, name: str = "", arguments: dict | None = None, rpc_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if name:
        payload["params"] = {"name": name, "arguments": arguments or {}}
    return payload


def _initialize_body(rpc_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "chatgpt-test", "version": "1.0"},
        },
    }


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "rootA").mkdir(parents=True)
    (tmp_path / "rootA" / "sub").mkdir(parents=True)
    (tmp_path / "rootB").mkdir(parents=True)
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    save_projects(
        [
            ProjectConfig(id=PROJ_A_ID, display_name="项目A", root_path=str(root_a), permission_mode="workspace"),
            ProjectConfig(id=PROJ_B_ID, display_name="项目B", root_path=str(root_b), permission_mode="workspace"),
        ]
    )
    fake = FakeUpstream()
    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root_a),
        upstream_legacy_token=lambda: PUB_TOKEN,
        allow_local_anonymous=True,
        transport=httpx.MockTransport(fake.handler),
        workspace_registry=lambda pid: {
            PROJ_A_ID: (9111, str(root_a)),
            PROJ_B_ID: (9222, str(root_b)),
        }.get(pid),
    )
    client = TestClient(gateway.app, raise_server_exceptions=False)
    return {"gateway": gateway, "client": client, "fake": fake, "root_a": root_a, "root_b": root_b, "tmp": tmp_path}


_LOOPBACK_HEADERS = {"x-forwarded-for": "127.0.0.1"}


def _post(client: TestClient, payload: dict, session_id: str = "") -> httpx.Response:
    headers = dict(_LOOPBACK_HEADERS)
    if session_id:
        headers["mcp-session-id"] = session_id
    return client.post("/mcp", json=payload, headers=headers)


def _session_id(response: httpx.Response) -> str:
    return response.headers.get("mcp-session-id", "")


def _open_calls(fake: FakeUpstream, session_id: str = "") -> list[dict]:
    return [
        call
        for call in fake.calls
        if call["method"] == "tools/call"
        and call["name"] == "open_workspace"
        and (not session_id or call["sid"] == session_id)
    ]


def test_open_workspace_replayed_on_fresh_transport(env: dict) -> None:
    client, fake = env["client"], env["fake"]
    root = env["root_a"]

    first = _post(client, _initialize_body())
    assert _session_id(first) == "engine-1"
    opened = _post(
        client,
        _mcp_json("tools/call", "open_workspace", {"root": str(root)}),
        session_id="engine-1",
    )
    assert opened.status_code == 200

    second = _post(client, _initialize_body(rpc_id=2))
    assert _session_id(second) == "engine-2"

    replays = _open_calls(fake, "engine-2")
    assert len(replays) == 1, fake.calls
    assert replays[0]["arguments"]["root"] == str(root)
    assert replays[0]["arguments"]["include_tree"] is False

    git = _post(client, _mcp_json("tools/call", "git_status", {}, rpc_id=3), session_id="engine-2")
    assert git.status_code == 200

    replay_index = fake.calls.index(replays[0])
    git_index = fake.calls.index(
        next(call for call in fake.calls if call["method"] == "tools/call" and call["name"] == "git_status" and call["sid"] == "engine-2")
    )
    assert replay_index < git_index


def test_open_root_persisted_to_disk(env: dict) -> None:
    client = env["client"]
    root = env["root_a"]
    first = _post(client, _initialize_body())
    _post(client, _mcp_json("tools/call", "open_workspace", {"root": str(root)}), session_id=_session_id(first))
    bindings_path = env["tmp"] / "cfg" / "gateway_workspace_binding.json"
    assert bindings_path.exists()
    raw = json.loads(bindings_path.read_text(encoding="utf-8"))
    assert raw["open_roots"]["loopback"]["root"] == str(root)
    assert raw["workspaces"]["loopback"]["id"] == PROJ_A_ID


def test_switch_workspace_pins_new_transports(env: dict) -> None:
    client, fake = env["client"], env["fake"]
    first = _post(client, _initialize_body())
    sid = _session_id(first)
    switched = _post(
        client, _mcp_json("tools/call", "devbridge_switch_workspace", {"project_id": PROJ_B_ID}), session_id=sid
    )
    assert switched.status_code == 200
    assert "bbbb2222" in switched.json()["result"]["content"][0]["text"]

    second = _post(client, _initialize_body(rpc_id=2))
    sid2 = _session_id(second)
    tree = _post(client, _mcp_json("tools/call", "tree", {}, rpc_id=3), session_id=sid2)
    assert tree.status_code == 200
    tree_call = next(
        call
        for call in fake.calls
        if call["method"] == "tools/call" and call["name"] == "tree" and call["sid"] == sid2
    )
    assert tree_call["port"] == 9222


def test_explicit_route_arg_pins_new_transports(env: dict) -> None:
    client, fake = env["client"], env["fake"]
    first = _post(client, _initialize_body())
    _post(
        client,
        _mcp_json("tools/call", "tree", {"devbridge_workspace_id": PROJ_B_ID}),
        session_id=_session_id(first),
    )
    second = _post(client, _initialize_body(rpc_id=2))
    sid2 = _session_id(second)
    search = _post(client, _mcp_json("tools/call", "search", {}, rpc_id=3), session_id=sid2)
    assert search.status_code == 200
    search_call = next(
        call
        for call in fake.calls
        if call["method"] == "tools/call" and call["name"] == "search" and call["sid"] == sid2
    )
    assert search_call["port"] == 9222


def test_open_workspace_routes_to_best_matching_running_project(env: dict) -> None:
    client, fake = env["client"], env["fake"]
    subdir = env["root_a"] / "sub"
    first = _post(client, _initialize_body())
    opened = _post(
        client, _mcp_json("tools/call", "open_workspace", {"root": str(subdir)}), session_id=_session_id(first)
    )
    assert opened.status_code == 200
    open_call = next(call for call in fake.calls if call["method"] == "tools/call" and call["name"] == "open_workspace")
    assert open_call["port"] == 9111
    assert open_call["arguments"]["root"] == str(subdir)


def test_open_current_workspace_rewritten_to_recorded_root(env: dict) -> None:
    client, fake = env["client"], env["fake"]
    root = env["root_a"]
    first = _post(client, _initialize_body())
    _post(client, _mcp_json("tools/call", "open_workspace", {"root": str(root)}), session_id=_session_id(first))
    second = _post(client, _initialize_body(rpc_id=2))
    sid2 = _session_id(second)
    reopened = _post(client, _mcp_json("tools/call", "open_current_workspace", {}, rpc_id=3), session_id=sid2)
    assert reopened.status_code == 200
    opens_on_new_session = _open_calls(fake, sid2)
    # First is the gateway's replay (include_tree=False); the client's
    # open_current_workspace must arrive rewritten as open_workspace(root).
    assert len(opens_on_new_session) == 2, fake.calls
    assert opens_on_new_session[-1]["arguments"] == {"root": str(root)}


def test_switch_workspace_binding_survives_gateway_restart(env: dict) -> None:
    client = env["client"]
    first = _post(client, _initialize_body())
    _post(
        client,
        _mcp_json("tools/call", "devbridge_switch_workspace", {"project_id": PROJ_B_ID}),
        session_id=_session_id(first),
    )
    fake2 = FakeUpstream()
    gateway2 = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(env["root_a"]),
        upstream_legacy_token=lambda: PUB_TOKEN,
        allow_local_anonymous=True,
        transport=httpx.MockTransport(fake2.handler),
        workspace_registry=lambda pid: {
            PROJ_A_ID: (9111, str(env["root_a"])),
            PROJ_B_ID: (9222, str(env["root_b"])),
        }.get(pid),
    )
    assert gateway2._client_workspace_binding("loopback") == PROJ_B_ID


def test_running_project_for_path_picks_longest_root(env: dict) -> None:
    gateway = env["gateway"]
    assert gateway._running_project_for_path(str(env["root_a"] / "sub" / "x")) == PROJ_A_ID
    assert gateway._running_project_for_path(str(env["root_b"])) == PROJ_B_ID
    assert gateway._running_project_for_path(str(env["tmp"] / "elsewhere")) == ""


def test_normalize_open_root() -> None:
    from local_dev_mcp_bridge.gateway import _normalize_open_root

    assert _normalize_open_root("") == ""
    assert _normalize_open_root("no/such/dir-anywhere") == ""
    assert _normalize_open_root("relative/path") == ""
