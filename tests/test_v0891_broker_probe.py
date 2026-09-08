"""A failed control-plane observation must not kill a healthy data plane."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import local_dev_mcp_bridge.elevation as elevation
from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.project_manager import ProjectManager


class Probe:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = True
        self.running = True

    def child_status(self, _project_id: str) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("elevated broker unavailable: TimeoutError")
        return {"running": self.running, "error": "process exited" if not self.running else ""}


@pytest.fixture
def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    probe = Probe()
    clock = [1000.0]
    monkeypatch.setattr(elevation, "get_elevation_controller", lambda: probe)
    monkeypatch.setattr(elevation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    manager = elevation.ElevatedCodexProManager("fixture", log_dir=tmp_path, port=18787)
    manager._state = EngineState.READY
    manager._pid = 123
    return manager, probe, clock


def test_transient_query_does_not_poison_state_and_later_recovers(control) -> None:
    manager, probe, clock = control
    assert manager.state == EngineState.READY
    assert manager.pid == 123
    assert manager.error is not None
    assert manager.state == EngineState.READY
    assert probe.calls == 1  # Backoff: another getter must not retry immediately.
    clock[0] += 2
    probe.fail = False
    assert manager.state == EngineState.READY
    assert manager.error is None
    assert probe.calls == 2


def test_confirmed_exit_is_still_a_hard_error(control) -> None:
    manager, probe, _clock = control
    probe.fail = False
    probe.running = False
    assert manager.state == EngineState.ERROR
    assert manager.error == "process exited"


def test_starting_query_failure_never_fabricates_ready(control) -> None:
    manager, _probe, _clock = control
    manager._state = EngineState.STARTING
    assert manager.state == EngineState.STARTING


@pytest.mark.parametrize("data_healthy", [True, False])
def test_supervisor_checks_data_plane_before_restarting(
    control, monkeypatch: pytest.MonkeyPatch, data_healthy: bool
) -> None:
    manager, _probe, _clock = control
    probes: list[bool] = []
    restarts: list[str] = []

    class Unit:
        @property
        def state(self):
            return manager.state

        @property
        def message(self):
            return manager.error

        def data_plane_health(self, _value: str):
            probes.append(data_healthy)
            return data_healthy, "ok" if data_healthy else "data plane unavailable"

    # Isolated supervisor state: no constructors that load real roots or credentials.
    supervisor = object.__new__(ProjectManager)
    supervisor._lock = threading.Lock()
    supervisor._runtime_specs = {
        "fixture": cast(Any, SimpleNamespace(codex_token="[REDACTED_SECRET]"))
    }
    supervisor._health_failures = {"fixture": 1}
    supervisor._last_restart = {}
    unit = Unit()
    monkeypatch.setattr(ProjectManager, "unit", lambda *_: unit)
    monkeypatch.setattr(ProjectManager, "_write_supervisor_event", lambda *_, **__: None)
    monkeypatch.setattr(
        ProjectManager,
        "_recover_project",
        lambda _self, project_id, _reason: restarts.append(project_id),
    )
    supervisor._supervisor_tick()
    assert probes == [data_healthy]
    assert restarts == ([] if data_healthy else ["fixture"])
