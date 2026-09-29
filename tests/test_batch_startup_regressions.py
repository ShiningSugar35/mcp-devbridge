from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from local_dev_mcp_bridge import elevation
from local_dev_mcp_bridge.batch_lifecycle import run_start_batch
from local_dev_mcp_bridge.engines import EngineState


class _SlowManager:
    created = 0
    started_event = threading.Event()
    release_event = threading.Event()

    def __init__(self, *, log_dir: Path, port: int) -> None:
        del log_dir
        type(self).created += 1
        self.port = port
        self.state = EngineState.IDLE
        self.error: str | None = None
        self.pid = 41000 + type(self).created

    @property
    def is_running(self) -> bool:
        return self.state in (EngineState.STARTING, EngineState.READY)

    def start(self, *_args, **_kwargs) -> None:
        self.state = EngineState.STARTING
        type(self).started_event.set()

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        type(self).release_event.wait(timeout=2)
        self.state = EngineState.READY
        return True

    def stop(self, timeout_seconds: float = 8.0) -> None:
        del timeout_seconds
        self.state = EngineState.IDLE

    def log_tail(self, count: int = 200) -> str:
        del count
        return ""


@pytest.fixture(autouse=True)
def _reset_slow_manager() -> None:
    _SlowManager.created = 0
    _SlowManager.started_event = threading.Event()
    _SlowManager.release_event = threading.Event()


def _payload(tmp_path: Path, project_id: str, port: int) -> dict[str, object]:
    root = tmp_path / project_id
    root.mkdir(exist_ok=True)
    return {
        "project_id": project_id,
        "root": str(root),
        "access_value": "x" * 32,
        "bridge_value": "",
        "port": port,
        "extra_env": {},
    }


def test_broker_slow_start_does_not_block_unrelated_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = elevation._BrokerRuntime("a" * 40)
    monkeypatch.setattr(elevation, "CodexProManager", _SlowManager)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    monkeypatch.setattr(runtime, "_assign_pid_to_job", lambda _pid: None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        spawn = executor.submit(runtime.spawn_codex, _payload(tmp_path, "slow", 19001))
        assert _SlowManager.started_event.wait(timeout=1)
        status = executor.submit(runtime.child_status, "other")
        try:
            result = status.result(timeout=0.2)
        except FutureTimeoutError:
            _SlowManager.release_event.set()
            spawn.result(timeout=2)
            pytest.fail("unrelated child_status was blocked by another project's readiness wait")
        else:
            assert result["exists"] is False
            _SlowManager.release_event.set()
            spawn.result(timeout=2)


def test_broker_pre_spawn_reservation_is_visible_to_idle_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_entered = threading.Event()
    release_start = threading.Event()

    class BlockingStartManager(_SlowManager):
        def start(self, *_args, **_kwargs) -> None:
            start_entered.set()
            assert release_start.wait(timeout=2)
            self.state = EngineState.STARTING

    runtime = elevation._BrokerRuntime("a" * 40)
    monkeypatch.setattr(elevation, "CodexProManager", BlockingStartManager)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    monkeypatch.setattr(runtime, "_assign_pid_to_job", lambda _pid: None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        spawn = executor.submit(runtime.spawn_codex, _payload(tmp_path, "pre-spawn", 19004))
        assert start_entered.wait(timeout=1)
        assert runtime.has_running_children() is True
        release_start.set()
        _SlowManager.release_event.set()
        spawn.result(timeout=2)


def test_broker_in_progress_start_is_visible_to_idle_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = elevation._BrokerRuntime("a" * 40)
    monkeypatch.setattr(elevation, "CodexProManager", _SlowManager)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    monkeypatch.setattr(runtime, "_assign_pid_to_job", lambda _pid: None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        spawn = executor.submit(runtime.spawn_codex, _payload(tmp_path, "starting", 19003))
        assert _SlowManager.started_event.wait(timeout=1)
        assert runtime.has_running_children() is True
        status = runtime.child_status("starting")
        assert status["exists"] is True
        assert status["running"] is True
        assert status["state"] == EngineState.STARTING.value
        _SlowManager.release_event.set()
        spawn.result(timeout=2)


def test_broker_parallel_same_project_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = elevation._BrokerRuntime("a" * 40)
    monkeypatch.setattr(elevation, "CodexProManager", _SlowManager)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    monkeypatch.setattr(runtime, "_assign_pid_to_job", lambda _pid: None)
    payload = _payload(tmp_path, "same", 19002)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runtime.spawn_codex, payload)
        assert _SlowManager.started_event.wait(timeout=1)
        second = executor.submit(runtime.spawn_codex, payload)
        _SlowManager.release_event.set()
        a = first.result(timeout=2)
        b = second.result(timeout=2)

    assert _SlowManager.created == 1
    assert a["pid"] == b["pid"]


def test_batch_first_success_hook_does_not_wait_for_slow_peer() -> None:
    slow_started = threading.Event()
    release_slow = threading.Event()
    hook_called = threading.Event()

    def start_one(item: str) -> str:
        if item == "slow":
            slow_started.set()
            assert release_slow.wait(timeout=2)
        return item

    with ThreadPoolExecutor(max_workers=1) as executor:
        batch = executor.submit(
            run_start_batch,
            ["slow", "fast"],
            start_one,
            on_first_success=hook_called.set,
            max_workers=2,
        )
        assert slow_started.wait(timeout=1)
        assert hook_called.wait(timeout=0.5), "first healthy root must not wait for the slow peer"
        release_slow.set()
        result = batch.result(timeout=2)

    assert {item for item, _value in result.started} == {"slow", "fast"}
    assert result.failures == ()


def test_batch_transport_hook_failure_does_not_rollback_started_projects() -> None:
    def fail_transport() -> None:
        raise RuntimeError("transport unavailable")

    result = run_start_batch(["a", "b"], lambda item: f"ready:{item}", on_first_success=fail_transport)
    assert len(result.started) == 2
    assert result.failures == ()
    assert isinstance(result.first_success_hook_error, RuntimeError)
