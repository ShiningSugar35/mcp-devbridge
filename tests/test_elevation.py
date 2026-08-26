from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_mcp_bridge import elevation


def test_registration_is_one_time(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = elevation.ElevationController()
    called = {"uac": 0}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_exists", lambda: True)

    def run_uac() -> bool:
        called["uac"] += 1
        return True

    monkeypatch.setattr(elevation, "_run_registration_uac", run_uac)
    assert controller.ensure_registered(interactive=True) is True
    assert called["uac"] == 0


def test_noninteractive_registration_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = elevation.ElevationController()
    called = {"uac": 0}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_exists", lambda: False)

    def run_uac() -> bool:
        called["uac"] += 1
        return True

    monkeypatch.setattr(elevation, "_run_registration_uac", run_uac)
    assert controller.ensure_registered(interactive=False) is False
    assert called["uac"] == 0


def test_elevated_registration_uses_highest_interactive_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    monkeypatch.setattr(elevation, "_task_exists", lambda: True)
    monkeypatch.setattr(
        elevation,
        "_broker_command",
        lambda: (
            r"C:\Program Files\MCP DevBridge\MCPDevBridge.exe",
            "--elevated-broker",
            r"C:\Program Files\MCP DevBridge",
        ),
    )

    class Result:
        returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> Result:
        seen["args"] = args
        return Result()

    monkeypatch.setattr(elevation.subprocess, "run", fake_run)
    assert elevation._register_task_current_process() is True
    args = seen["args"]
    assert isinstance(args, list)
    text = str(args[-1])
    assert "-RunLevel Highest" in text
    assert "-LogonType Interactive" in text
    assert "--elevated-broker" in text
    assert "DisableLUA" not in text
    assert "fodhelper" not in text.lower()


def test_uac_registration_elevates_current_executable_not_writable_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(
        elevation,
        "_registration_command",
        lambda: (
            r"C:\Program Files\MCP DevBridge\MCPDevBridge.exe",
            ["--register-elevated-broker-task"],
            r"C:\Program Files\MCP DevBridge",
        ),
    )
    monkeypatch.setattr(elevation, "_task_exists", lambda: True)

    class Result:
        returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> Result:
        seen["args"] = args
        return Result()

    monkeypatch.setattr(elevation.subprocess, "run", fake_run)
    assert elevation._run_registration_uac() is True
    args = seen["args"]
    assert isinstance(args, list)
    command = str(args[-1])
    assert "--register-elevated-broker-task" in command
    assert "register-elevated-broker.ps1" not in command


def test_state_file_never_contains_broker_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(elevation, "_state_path", lambda: state)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)
    elevation._write_state(45678, "epoch-test")
    text = state.read_text(encoding="utf-8")
    assert "45678" in text
    assert "epoch-test" in text
    assert elevation._auth_store_key() not in text


def test_broker_execute_rejects_missing_cwd(tmp_path: Path) -> None:
    runtime = elevation._BrokerRuntime("x" * 40)
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="cwd"):
        runtime.execute(
            {
                "kind": "command",
                "command": "Write-Output ok",
                "cwd": str(missing),
                "timeout_seconds": 1,
            }
        )


def test_controller_refuses_false_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = elevation.ElevationController()
    monkeypatch.setattr(controller, "health", lambda: {"ok": True, "elevated": False})
    monkeypatch.setattr(controller, "ensure_registered", lambda *, interactive: True)
    monkeypatch.setattr(elevation, "_run_task", lambda: True)
    monkeypatch.setattr(elevation, "_state_path", lambda: Path("Z:/definitely/missing/state.json"))
    monkeypatch.setattr(elevation.time, "sleep", lambda _seconds: None)
    health_calls = iter([None, {"ok": True, "elevated": False}])
    monkeypatch.setattr(controller, "health", lambda: next(health_calls))
    with pytest.raises(RuntimeError, match="未处于 elevated"):
        controller.ensure_running(interactive_registration=False)


def test_elevated_manager_coalesces_status_ipc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"status": 0}

    class FakeController:
        def child_status(self, _project_id: str) -> dict[str, object]:
            calls["status"] += 1
            return {"running": True, "state": "已连接"}

        def log_tail(self, _project_id: str, _count: int) -> str:
            return ""

    monkeypatch.setattr(elevation, "get_elevation_controller", lambda: FakeController())
    manager = elevation.ElevatedCodexProManager("p1", log_dir=tmp_path, port=12345)
    manager._state = elevation.EngineState.READY
    assert manager.state == elevation.EngineState.READY
    assert manager.state == elevation.EngineState.READY
    assert calls["status"] == 1


def test_broker_running_child_count_uses_runtime_lock() -> None:
    runtime = elevation._BrokerRuntime("x" * 40)
    assert runtime.running_child_count() == 0


def test_ensure_running_serializes_parallel_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    controller = elevation.ElevationController()
    started = {"value": False, "runs": 0}
    monkeypatch.setattr(controller, "ensure_registered", lambda *, interactive: True)
    monkeypatch.setattr(elevation, "_state_path", lambda: tmp_path / "broker-state.json")
    monkeypatch.setattr(
        controller,
        "health",
        lambda: {"ok": True, "elevated": True} if started["value"] else None,
    )

    def fake_run_task() -> bool:
        started["runs"] += 1
        started["value"] = True
        return True

    monkeypatch.setattr(elevation, "_run_task", fake_run_task)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _i: controller.ensure_running(), range(2)))
    assert all(result["elevated"] is True for result in results)
    assert started["runs"] == 1


def test_elevated_manager_stop_failure_is_not_reported_idle(tmp_path: Path) -> None:
    class FailingController:
        def child_status(self, _project_id: str) -> dict[str, object]:
            return {"running": True}

        def stop_child(self, _project_id: str) -> None:
            raise RuntimeError("broker unavailable")

        def log_tail(self, _project_id: str, _count: int) -> str:
            return ""

    manager = elevation.ElevatedCodexProManager("p1", log_dir=tmp_path, port=12345)
    manager._controller = FailingController()  # type: ignore[assignment]
    manager._state = elevation.EngineState.READY
    with pytest.raises(elevation.SpawnError, match="停止高权限 CodexPro 失败"):
        manager.stop()
    assert manager.state == elevation.EngineState.ERROR
