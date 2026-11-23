"""Regression tests for the independent temporary-device MCP endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from local_dev_mcp_bridge.live_agent import serve_agent
from local_dev_mcp_bridge.live_server import DeviceSlot, LiveError
from local_dev_mcp_bridge.tools import LocalDevTools

_GUI_TEST_KEY = "temporary-gui-fixture-key"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def live_server(tmp_path: Path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    owner = secrets.token_urlsafe(32)
    access = secrets.token_urlsafe(32)
    endpoint = f"http://127.0.0.1:{port}"
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(_GUI_TEST_KEY.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    verifier = f"scrypt-v1:{salt.hex()}:{digest.hex()}"
    log_path = tmp_path / "service.log"
    with (
        log_path.open("wb") as log,
        subprocess.Popen(
            [sys.executable, "-B", "-m", "local_dev_mcp_bridge.live_server", "--port", str(port)],
            env={
                **os.environ,
                "MCP_LIVE_OWNER_TOKEN": owner,
                "MCP_LIVE_ACCESS_TOKEN": access,
                "MCP_LIVE_DEVICE_KEY_VERIFIER": verifier,
            },
            stdout=log,
            stderr=subprocess.STDOUT,
        ) as process,
    ):
        try:
            with httpx.Client(base_url=endpoint, trust_env=False, timeout=0.25) as client:
                for _ in range(80):
                    if process.poll() is not None:
                        pytest.fail(log_path.read_text(errors="replace"))
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("Live endpoint failed to start")
            yield endpoint, owner, access
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=8)


async def invoke(session: ClientSession, name: str, **arguments):
    result = await session.call_tool(name, arguments)
    return (
        bool(getattr(result, "is_error", False) or getattr(result, "isError", False)),
        getattr(result, "structured_content", None) or getattr(result, "structuredContent", None),
        "".join(getattr(item, "text", "") for item in result.content),
    )


async def wait_online(session: ClientSession) -> str:
    for _ in range(80):
        error, status, _ = await invoke(session, "live_status")
        assert not error and status is not None
        if status["online"]:
            return status["device_handle"]
        await asyncio.sleep(0.04)
    pytest.fail("The agent did not connect")


async def test_same_mcp_session_moves_from_a_to_offline_to_b(live_server, tmp_path: Path):
    endpoint, owner, access = live_server
    a = tmp_path / "Windows 模拟 A"
    b = tmp_path / "Server 模拟 B"
    for root, marker in [(a, "machine-A"), (b, "machine-B")]:
        root.mkdir()
        (root / "hostname.txt").write_text(marker, encoding="utf-8")
        (root / ".env").write_text("test-only", encoding="utf-8")
        (root / ".env.test").write_text("test-only", encoding="utf-8")
        (root / "private.pem").write_text("test-only", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")

    async with (
        httpx.AsyncClient(base_url=endpoint, trust_env=False) as admin,
        httpx2.AsyncClient(headers=bearer(access), trust_env=False) as wire,
        streamable_http_client(endpoint + "/mcp", http_client=wire) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        assert (await session.initialize()).server_info.name == "mcp-live"
        tools = await session.list_tools()
        assert {"live_status", "live_read"} <= {tool.name for tool in tools.tools}
        assert (await invoke(session, "live_status"))[1] == {"online": False}
        assert (await admin.post("/mcp", json={})).status_code == 401
        assert (await admin.post("/mcp", headers=bearer(owner), json={})).status_code == 401
        request_body = {"name": "test"}
        unsigned = await admin.post("/live/connect", json=request_body)
        assert unsigned.status_code == 401
        assert (await admin.post("/live/pair", headers=bearer(access))).status_code == 401

        code = (await admin.post("/live/pair", headers=bearer(owner))).json()["pair_code"]
        stop_a = asyncio.Event()
        agent_a = asyncio.create_task(serve_agent(endpoint, code, "A", a, stop=stop_a))
        try:
            handle_a = await wait_online(session)
            error, output, _ = await invoke(
                session, "live_read", device_handle=handle_a, path="hostname.txt"
            )
            assert not error and output is not None and "machine-A" in output["text"]
            assert (
                await admin.post("/live/connect", headers=bearer(code), json=request_body)
            ).status_code == 401
            code_b = (await admin.post("/live/pair", headers=bearer(owner))).json()["pair_code"]
            assert (
                await admin.post("/live/connect", headers=bearer(code_b), json=request_body)
            ).status_code == 409
        finally:
            stop_a.set()
            await asyncio.wait_for(agent_a, 12)
        assert (await invoke(session, "live_status"))[1] == {"online": False}
        assert (await invoke(session, "live_read", device_handle=handle_a, path="hostname.txt"))[0]

        stop_b = asyncio.Event()
        agent_b = asyncio.create_task(serve_agent(endpoint, code_b, "B", b, stop=stop_b))
        try:
            handle_b = await wait_online(session)
            assert handle_a != handle_b
            assert (await invoke(session, "live_read", device_handle=handle_a, path="hostname.txt"))[0]
            for path in (
                "../outside.txt",
                ".env",
                ".env.test",
                "private.pem",
                r"C:\outside.txt",
                "hostname.txt:stream",
            ):
                assert (await invoke(session, "live_read", device_handle=handle_b, path=path))[0]
            error, output, _ = await invoke(
                session, "live_read", device_handle=handle_b, path="hostname.txt"
            )
            assert not error and output is not None and "machine-B" in output["text"]
            assert (await session.list_tools()).tools == tools.tools
        finally:
            stop_b.set()
            await asyncio.wait_for(agent_b, 12)


async def test_expiry_and_stale_device_results():
    now = 50.0
    slot = DeviceSlot(clock=lambda: now)
    grant = slot.issue_code()
    now += 601
    with pytest.raises(LiveError, match="配对码"):
        slot.connect(grant, "expired")
    token_a = slot.connect(slot.issue_code(), "A")
    handle = slot.status()["device_handle"]
    read = asyncio.create_task(slot.read(str(handle), "fixture.txt"))
    operation = await slot.next_operation(token_a)
    assert operation is not None
    queued = asyncio.create_task(slot.read(str(handle), "another.txt"))
    await asyncio.sleep(0)
    assert not queued.done()
    now += 46
    token_b = slot.connect(slot.issue_code(), "B")
    assert token_b != token_a
    with pytest.raises(LiveError, match="离线"):
        await read
    with pytest.raises(LiveError, match="离线"):
        await queued
    with pytest.raises(LiveError, match="设备凭据"):
        slot.finish(token_a, str(operation["id"]), "stale", None)
    with pytest.raises(LiveError, match="请求已结束"):
        slot.finish(token_b, str(operation["id"]), "stale", None)


async def test_one_pending_poll_and_owner_revocation():
    slot = DeviceSlot()
    token = slot.connect(slot.issue_code(), "A")
    pending = asyncio.create_task(slot.next_operation(token))
    await asyncio.sleep(0)
    with pytest.raises(LiveError, match="已有在线请求"):
        await slot.next_operation(token)
    slot.revoke()
    with pytest.raises(LiveError, match="设备凭据"):
        await pending
    assert slot.status() == {"online": False}


def test_rejects_file_that_grows_after_size_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "changing.txt"
    target.write_text("small", encoding="utf-8")
    tools = LocalDevTools(tmp_path, "read_only", max_file_bytes=10)
    open_path = Path.open

    def grow_before_open(self: Path, mode: str = "r", *args, **kwargs):
        if self == target and mode == "rb":
            with open_path(target, "wb") as file:
                file.write(b"01234567890")
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_before_open)
    with pytest.raises(ValueError, match="文件超过读取上限"):
        tools.read_file("changing.txt")


def test_oauth_protects_mcp_and_binds_tokens_to_the_live_resource(monkeypatch, tmp_path):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from local_dev_mcp_bridge.constants import OAUTH_SCOPE
    from local_dev_mcp_bridge.live_oauth import LiveOAuth
    from local_dev_mcp_bridge.live_server import create_app

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    owner, access, approval = (secrets.token_urlsafe(32) for _ in range(3))
    origin = "https://live.example.test"
    resource = origin + "/mcp"
    callback = "https://chatgpt.com/connector/oauth/test-registration"
    oauth = LiveOAuth(origin, approval)
    app = create_app(owner, access, "live.example.test", oauth=oauth)
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    protocol_headers = {"Accept": "application/json, text/event-stream"}
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "live_status", "arguments": {}},
    }
    with TestClient(app, base_url=origin, follow_redirects=False) as client:
        unauthenticated = client.post("/mcp", json=request, headers=protocol_headers)
        assert unauthenticated.status_code == 401
        assert f'resource_metadata="{origin}{metadata_path}"' in unauthenticated.headers[
            "WWW-Authenticate"
        ]
        resource_metadata = client.get(metadata_path).json()
        assert resource_metadata["resource"] == resource
        assert resource_metadata["authorization_servers"] == [origin + "/"]
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200
        registration = {
            "redirect_uris": [callback],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        attacker = {**registration, "redirect_uris": ["https://untrusted.example/oauth"]}
        assert client.post("/register", json=attacker).status_code == 400
        registered = client.post("/register", json=registration)
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]
        client_secret = registered.json()["client_secret"]

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        params = {
            "client_id": client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "state": secrets.token_urlsafe(16),
            "code_challenge": challenge.rstrip("="),
            "code_challenge_method": "S256",
            "resource": resource,
        }
        invalid = client.get("/authorize", params={**params, "resource": origin + "/other"})
        assert "error=invalid_target" in invalid.headers.get("location", "")
        prompt = client.get("/authorize", params=params)
        consent_page = prompt.headers["location"]
        assert consent_page.startswith("/consent?id=")
        page = client.get(consent_page)
        assert page.status_code == 200
        assert "form-action 'self' https://chatgpt.com;" in page.headers["Content-Security-Policy"]
        consent_id = parse_qs(urlparse(consent_page).query)["id"][0]
        form = {"id": consent_id, "credential": owner}
        assert client.post("/consent", data=form).status_code == 401
        authorized = client.post("/consent", data={**form, "credential": approval})
        assert authorized.status_code == 303
        location = urlparse(authorized.headers["location"])
        assert location._replace(query="").geturl() == callback
        redirect = parse_qs(location.query)
        assert redirect["state"] == [params["state"]]
        code = redirect["code"][0]
        assert client.post("/consent", data={**form, "credential": approval}).status_code == 410
        token_form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": callback,
            "code_verifier": verifier,
            "resource": resource,
        }
        exchanged = client.post("/token", data=token_form)
        assert exchanged.status_code == 200, exchanged.text
        assert client.post("/token", data=token_form).status_code == 400
        token = exchanged.json()["access_token"]
        for method, value in (("OAuth", token), ("legacy", access)):
            result = client.post("/mcp", json=request, headers={**protocol_headers, **bearer(value)})
            assert result.status_code == 200, method
            body = result.json()
            assert not body["result"].get("isError"), method
            assert body["result"]["structuredContent"]["online"] is False
            assert client.post("/live/pair", headers=bearer(value)).status_code == 401
        rejected = client.post("/mcp", json=request, headers={**protocol_headers, **bearer(owner)})
        assert rejected.status_code == 401
        refresh_form = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": exchanged.json()["refresh_token"],
            "resource": resource,
        }
        assert client.post("/token", data=refresh_form).status_code == 200
        assert client.post("/token", data=refresh_form).status_code == 400


async def test_portable_window_connects_and_disconnects(live_server, tmp_path, monkeypatch):
    import tkinter as tk

    from local_dev_mcp_bridge import live_desktop
    from local_dev_mcp_bridge.live_profile import load_profile

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "credential-test"))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk desktop is unavailable")
    root.withdraw()
    window = live_desktop.LiveWindow(root, bridge_enabled=False)
    endpoint, owner, access = live_server
    folder = tmp_path / "GUI 共享目录"
    folder.mkdir()
    (folder / "验证.txt").write_text("mcp-live-gui-smoke", encoding="utf-8")
    (folder / ".env").write_text("test-secret", encoding="utf-8")
    second = tmp_path / "另一个父目录" / folder.name
    second.mkdir(parents=True)
    (second / "验证.txt").write_text("mcp-live-gui-second", encoding="utf-8")

    async def wait_for_status(expected: str) -> None:
        for _ in range(150):
            root.update()
            if window.status.get() == expected:
                return
            await asyncio.sleep(0.03)
        pytest.fail(f"expected {expected}, got {window.status.get()}: {window.detail.get()}")

    closed = False
    try:
        selections = iter([folder, second, second])
        monkeypatch.setattr(live_desktop.filedialog, "askdirectory", lambda **_: str(next(selections)))
        window.start_button.invoke()
        assert window.status.get() == "请补全连接信息"
        for _ in range(3):
            window.browse_button.invoke()
        alias_a, alias_b = tuple(window.folders)
        assert alias_a == folder.name
        assert alias_b == folder.name + "-2"
        assert set(load_profile()["folders"]) == {alias_a, alias_b}
        window.advanced_button.invoke()
        assert window.show_advanced.get()
        window.endpoint.set(endpoint)
        window.device_name.set("Windows 便携 GUI")

        window.code.set("invalid-pair-code")
        window.start_button.invoke()
        await wait_for_status("连接失败")
        assert "配对码" in window.detail.get()

        async with (
            httpx.AsyncClient(base_url=endpoint, trust_env=False) as admin,
            httpx2.AsyncClient(headers=bearer(access), trust_env=False) as wire,
            streamable_http_client(endpoint + "/mcp", http_client=wire) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            assert (await admin.post("/live/pair", headers=bearer(_GUI_TEST_KEY))).status_code == 401
            window.code.set(_GUI_TEST_KEY)
            window.start_button.invoke()
            await wait_for_status("已连接")
            assert str(window.browse_button.cget("state")) == "disabled"
            assert window.code.get() == ""
            assert _GUI_TEST_KEY not in repr(load_profile())
            assert "当前窗口已验证" in window.key_hint.get()
            handle = await wait_online(session)
            _, status, _ = await invoke(session, "live_status")
            assert status and status["folders"].splitlines() == [alias_a, alias_b]
            requested = [(alias_a, "mcp-live-gui-smoke"), (alias_b, "mcp-live-gui-second")]
            reads = await asyncio.gather(
                *(invoke(session, "live_read", path=f"{alias}/验证.txt", device_handle=handle)
                  for alias, _ in requested)
            )
            for (_, marker), (read_error, result, _) in zip(requested, reads, strict=True):
                assert not read_error and result and marker in result["text"]
            for rejected in [f"{alias_a}/.env", "未授权/验证.txt", "验证.txt", f"{alias_a}/../验证.txt"]:
                forbidden, _, _ = await invoke(
                    session, "live_read", path=rejected, device_handle=handle
                )
                assert forbidden

            window.stop_button.invoke()
            await wait_for_status("已断开")
            error, status, _ = await invoke(session, "live_status")
            assert not error and status == {"online": False}
            assert str(window.browse_button.cget("state")) == "normal"

            window.start_button.invoke()
            await wait_for_status("已连接")
            new_handle = await wait_online(session)
            assert new_handle != handle
            window.close()
            for _ in range(150):
                try:
                    root.update()
                    root.winfo_exists()
                except tk.TclError:
                    closed = True
                    break
                await asyncio.sleep(0.03)
            assert closed, "the window did not exit after disconnecting"
            error, status, _ = await invoke(session, "live_status")
            assert not error and status == {"online": False}
    finally:
        if not closed:
            window.close()
            for _ in range(300):
                try:
                    root.update()
                    root.winfo_exists()
                except tk.TclError:
                    break
                await asyncio.sleep(0.03)


async def test_live_native_hub_contract_and_permission_routes(live_server, tmp_path: Path):
    import re

    from mcp.types import Tool

    from local_dev_mcp_bridge.engines import find_node
    from local_dev_mcp_bridge.gateway import _STABLE_HUB_TOOLS

    if not find_node():
        pytest.skip("original CodexPro runtime is unavailable")
    endpoint, owner, access = live_server
    projects = {"readonly": tmp_path / "readonly", "workspace": tmp_path / "workspace"}
    for name, root in projects.items():
        root.mkdir()
        (root / "fixture.txt").write_text(f"LIVE-0.8.9-{name}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(projects["workspace"])], check=True)
    stop = asyncio.Event()
    async with (
        httpx.AsyncClient(base_url=endpoint, trust_env=False) as admin,
        httpx2.AsyncClient(headers=bearer(access), trust_env=False, timeout=45) as wire,
        streamable_http_client(endpoint + "/mcp", http_client=wire) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        by_name = {tool.name: tool for tool in listing.tools}
        assert len(by_name) == 52
        for raw in _STABLE_HUB_TOOLS:
            expected = Tool.model_validate(raw)
            assert by_name[expected.name].model_dump(by_alias=True) == expected.model_dump(by_alias=True)
        grant = await admin.post("/live/pair", headers=bearer(owner))
        grant.raise_for_status()
        agent = asyncio.create_task(
            serve_agent(
                endpoint, grant.json()["pair_code"], "Live 0.8.9 test", projects,
                stop=stop, bridge_permissions={"readonly": "read_only", "workspace": "workspace"},
            )
        )
        try:
            for _ in range(340):
                if agent.done():
                    await agent
                    pytest.fail("Live agent stopped before startup")
                _, status, _ = await invoke(session, "live_status")
                if status and status.get("bridge"):
                    break
                await asyncio.sleep(0.15)
            else:
                pytest.fail("native DevBridge never connected")

            err, _, workspaces = await invoke(session, "devbridge_list_workspaces")
            assert not err and "readonly" in workspaces and "workspace" in workspaces, workspaces
            routes = {}
            for line in workspaces.splitlines():
                match = re.search(r"\bid=(\S+)\s+(readonly|workspace)\s", line)
                if match:
                    routes[match.group(2)] = {"devbridge_workspace_id": match.group(1)}
            assert set(routes) == set(projects), workspaces

            readonly = routes["readonly"]
            workspace = routes["workspace"]
            err, _, result = await invoke(session, "read", **readonly, path="fixture.txt")
            assert not err and "LIVE-0.8.9-readonly" in result
            err, _, result = await invoke(session, "tree", **readonly, path=".")
            assert err and "只读" in result
            for tool, params in [
                ("tree", {"path": "."}),
                ("search", {"query": "LIVE-0.8.9-workspace", "path": "."}),
            ]:
                err, _, result = await invoke(session, tool, **workspace, **params)
                assert not err and "fixture.txt" in result, (tool, result)
            err, _, result = await invoke(
                session, "write", **readonly, path="denied.txt", content="test fixture"
            )
            assert err and "只读" in result
            assert not (projects["readonly"] / "denied.txt").exists()
            err, _, result = await invoke(
                session, "write", **workspace, path="write.txt", content="before edit"
            )
            assert not err, result
            err, _, result = await invoke(
                session, "edit", **workspace, path="write.txt", old_text="before", new_text="after"
            )
            assert not err, result
            assert (projects["workspace"] / "write.txt").read_text() == "after edit"
            err, _, result = await invoke(session, "git_status", **workspace, path=".")
            assert err and "完全访问" in result, result
            err, _, result = await invoke(session, "run_command", **workspace, command="git --version")
            assert not err and "git version" in result, result
            err, task, result = await invoke(session, "bash", **workspace, command="git --version")
            assert not err and task and task.get("task_id"), (task, result)
            err, _, result = await invoke(
                session, "wait_task", task_id=task["task_id"], wait_seconds=6
            )
            assert not err and "Status: completed" in result and "Exit: 0" in result, result
            err, _, result = await invoke(
                session, "windows_call", **workspace, tool="get_state", arguments={}
            )
            assert err, result
        finally:
            stop.set()
            await asyncio.wait_for(agent, 50)


def test_public_download_serves_only_the_configured_archive(tmp_path: Path):
    import zipfile

    from starlette.testclient import TestClient

    from local_dev_mcp_bridge.live_server import create_app

    archive = tmp_path / "MCP-Live-test.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("README.txt", "test fixture")
    (tmp_path / "owner-token").write_text("test-only sentinel", encoding="utf-8")
    app = create_app("o" * 32, "a" * 32, download_file=archive)
    with TestClient(app) as client:
        page = client.get("/download")
        assert page.status_code == 200
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert digest in page.text
        assert client.get("/download/windows").content == archive.read_bytes()
        assert client.head("/download/windows").headers["content-length"] == str(archive.stat().st_size)
        assert client.get("/download/owner-token").status_code == 404
        assert client.get("/mcp").status_code == 401
