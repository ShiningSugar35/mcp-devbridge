"""Blocking local calls must not stall the shared HTTP event loop."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import local_dev_mcp_bridge.gateway as gm


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(gm, "_LOG_DIR", tmp_path / "logs")
    gw = gm.OAuthGateway(public_hostname="fixture.test", workspace=str(tmp_path))
    monkeypatch.setattr(gw, "_workspace_permission_mode", lambda _id: "workspace")
    return gw


def result() -> Any:
    return SimpleNamespace(shell="fixture", command="fixture", exit_code=0,
                           duration_seconds=0.1, timed_out=False, stdout="ok", stderr="")


def params(name: str) -> dict[str, Any]:
    return {"name": name, "arguments": {"command": "echo fixture", "executable": "fixture", "timeout_seconds": 1}}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["run_command", "run_program"])
async def test_slow_local_tool_keeps_health_responsive(gateway, monkeypatch, name):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    worker_threads = []

    def slow(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        release.wait(1.5)
        return result()

    monkeypatch.setattr(gm, name, slow)
    task = asyncio.create_task(gateway._exec_local_tool(name, {"id": "local-1"}, params(name)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done(), "synchronous execution blocked the loop until the command ended"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://fixture") as client:
            response = await asyncio.wait_for(client.get("/health"), 0.5)
        assert response.status_code == 200
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != caller_thread
        release.set()
        response = await task
        assert json.loads(response.body)["id"] == "local-1"
    finally:
        release.set()
        await task
        await gateway._http.aclose()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_running_worker_capacity(gateway, monkeypatch):
    release = threading.Event()
    entered = 0
    lock = threading.Lock()

    def slow(*_args, **_kwargs):
        nonlocal entered
        with lock:
            entered += 1
        release.wait(3)
        return result()

    monkeypatch.setattr(gm, "run_command", slow)
    tasks = [asyncio.create_task(gateway._exec_local_tool("run_command", {"id": n}, params("run_command"))) for n in range(5)]
    try:
        for _ in range(100):
            with lock:
                count = entered
            if count == 5:
                break
            await asyncio.sleep(0.01)
        assert entered == 5
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        overloaded = await gateway._exec_local_tool("run_command", {"id": "busy"}, params("run_command"))
        body = json.loads(overloaded.body)
        assert body["id"] == "busy"
        assert body["error"]["code"] == -32005
        assert entered == 5  # Rejected calls never start or queue a command.
        release.set()
        await asyncio.gather(*tasks[1:])
        for _ in range(100):
            if not gateway._local_tool_workers:
                break
            await asyncio.sleep(0.01)
        assert not gateway._local_tool_workers
        followup = await gateway._exec_local_tool("run_command", {"id": "next"}, params("run_command"))
        assert "result" in json.loads(followup.body)
        assert entered == 6
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await gateway._http.aclose()


@pytest.mark.asyncio
async def test_local_worker_error_is_correlated_and_capacity_released(gateway, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("fixture execution failed")

    monkeypatch.setattr(gm, "run_command", broken)
    response = await gateway._exec_local_tool("run_command", {"id": "error-1"}, params("run_command"))
    body = json.loads(response.body)
    assert body["id"] == "error-1" and body["error"]["code"] == -32603
    await asyncio.sleep(0)
    assert not gateway._local_tool_workers
    await gateway._http.aclose()
