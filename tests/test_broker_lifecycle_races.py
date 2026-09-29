"""Deterministic race regressions; never start real processes or the elevated service."""
from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_dev_mcp_bridge import elevation
from local_dev_mcp_bridge.engines import EngineState, SpawnError


@pytest.fixture(autouse=True)
def no_real_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(elevation._BrokerRuntime, "_init_kill_on_close_job", lambda self: None)
    monkeypatch.setattr(elevation._BrokerRuntime, "_assign_pid_to_job", lambda self, pid: None)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)


def _payload(root: Path) -> dict[str, object]:
    return {"project_id": "race", "root": str(root), "port": 19010, "access_value": "x" * 32}


def test_gui_state_stays_pure_while_worker_refresh_is_in_flight(tmp_path: Path) -> None:
    manager = elevation.ElevatedCodexProManager("race", log_dir=tmp_path, port=19010)
    manager._state = EngineState.READY
    entered, release = threading.Event(), threading.Event()
    callers: list[int] = []

    def child_status(_project: str) -> dict[str, object]:
        callers.append(threading.get_ident())
        if len(callers) == 1:
            entered.set()
            assert release.wait(2)
        return {"running": True, "state": EngineState.READY.value}

    manager._controller = SimpleNamespace(child_status=child_status)  # type: ignore[assignment]
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(manager.refresh_status)
        try:
            assert entered.wait(1)
            assert manager.state == EngineState.READY
            assert len(callers) == 1, "GUI property entered IPC during an unrelated worker refresh"
        finally:
            release.set()
            refresh.result(timeout=2)


def test_late_refresh_cannot_overwrite_completed_stop(tmp_path: Path) -> None:
    manager = elevation.ElevatedCodexProManager("race", log_dir=tmp_path, port=19010)
    manager._state = EngineState.READY
    entered, release = threading.Event(), threading.Event()

    def child_status(_project: str) -> dict[str, object]:
        entered.set()
        assert release.wait(2)
        return {"running": False, "error": "old generation exited"}

    manager._controller = SimpleNamespace(  # type: ignore[assignment]
        child_status=child_status, stop_child=lambda project: None
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(manager.refresh_status)
        try:
            assert entered.wait(1)
            manager.stop()
            assert manager.state == EngineState.IDLE
        finally:
            release.set()
            refresh.result(timeout=2)
    assert manager.state == EngineState.IDLE, "stale observation overwrote a newer lifecycle state"
    assert manager.error is None


class _FakeManager:
    def __init__(self, **_kwargs: object) -> None:
        self.state = EngineState.IDLE
        self.error: str | None = None
        self.pid = 42001
        self.stops = 0

    @property
    def is_running(self) -> bool:
        return self.state in (EngineState.STARTING, EngineState.READY)

    def start(self, *_args: object, **_kwargs: object) -> None:
        self.state = EngineState.STARTING

    def wait_ready(self) -> bool:
        self.state = EngineState.READY
        return True

    def stop(self) -> None:
        self.stops += 1
        self.state = EngineState.IDLE


def test_stop_all_drains_pre_spawn_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
    manager = _FakeManager()

    def blocking_start(*_args: object, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(2)
        manager.state = EngineState.STARTING

    monkeypatch.setattr(manager, "start", blocking_start)
    monkeypatch.setattr(elevation, "CodexProManager", lambda **kwargs: manager)
    runtime = elevation._BrokerRuntime("a" * 40)

    def stop_all() -> None:
        stopping.set()
        runtime.stop_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        spawn = pool.submit(runtime.spawn_codex, _payload(tmp_path))
        stop = None
        try:
            assert entered.wait(1)
            stop = pool.submit(stop_all)
            assert stopping.wait(1)
            with pytest.raises(FutureTimeoutError):
                stop.result(timeout=0.1)
        finally:
            release.set()
            # A pending start may correctly be cancelled by closing admission.
            with contextlib.suppress(SpawnError):
                spawn.result(timeout=2)
            if stop is not None:
                stop.result(timeout=2)
    assert not runtime.has_running_children()
    assert manager.stops == 1
    assert runtime.children == {}


def test_failed_stop_retains_process_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    manager.state = EngineState.READY
    runtime = elevation._BrokerRuntime("a" * 40)
    runtime.children["race"] = manager  # type: ignore[assignment]

    def fail_stop() -> None:
        raise SpawnError("injected stop failure")

    monkeypatch.setattr(manager, "stop", fail_stop)
    with pytest.raises(SpawnError, match="injected stop failure"):
        runtime.stop_child("race")
    assert runtime.children.get("race") is manager
    assert runtime.has_running_children()


def test_failed_start_cleans_already_created_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()

    def fail_after_spawn(*_args: object, **_kwargs: object) -> None:
        manager.state = EngineState.STARTING
        raise SpawnError("injected failure after process creation")

    monkeypatch.setattr(manager, "start", fail_after_spawn)
    monkeypatch.setattr(elevation, "CodexProManager", lambda **kwargs: manager)
    runtime = elevation._BrokerRuntime("a" * 40)
    with pytest.raises(SpawnError, match="injected failure"):
        runtime.spawn_codex(_payload(tmp_path))
    assert manager.stops == 1
    assert not runtime.has_running_children()


def test_idle_shutdown_closes_spawn_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(elevation, "CodexProManager", lambda **kwargs: manager)
    runtime = elevation._BrokerRuntime("a" * 40)
    assert runtime.request_shutdown_if_idle() is True
    with pytest.raises(SpawnError, match="shut|stopp|clos"):
        runtime.spawn_codex(_payload(tmp_path))
    assert manager.state == EngineState.IDLE
