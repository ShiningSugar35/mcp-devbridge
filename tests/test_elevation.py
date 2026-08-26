from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from local_dev_mcp_bridge import elevation


def test_registration_is_one_time(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = elevation.ElevationController()
    called = {"uac": 0}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_matches_current_command", lambda: True)

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
    monkeypatch.setattr(elevation, "_task_matches_current_command", lambda: False)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: False)

    def run_uac() -> bool:
        called["uac"] += 1
        return True

    monkeypatch.setattr(elevation, "_run_registration_uac", run_uac)
    assert controller.ensure_registered(interactive=False) is False
    assert called["uac"] == 0


def test_already_elevated_registration_never_prompts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = elevation.ElevationController()
    called = {"register": 0, "uac": 0}
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_matches_current_command", lambda: False)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: True)

    def register() -> bool:
        called["register"] += 1
        return True

    def run_uac() -> bool:
        called["uac"] += 1
        return True

    monkeypatch.setattr(elevation, "_register_task_current_process", register)
    monkeypatch.setattr(elevation, "_run_registration_uac", run_uac)
    assert controller.ensure_registered(interactive=True) is True
    assert called == {"register": 1, "uac": 0}


def test_stale_registered_task_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = elevation.ElevationController()
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_matches_current_command", lambda: False)
    monkeypatch.setattr(elevation, "_token_is_elevated", lambda: False)
    monkeypatch.setattr(elevation, "_run_registration_uac", lambda: True)
    assert controller.ensure_registered(interactive=True) is True


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
    caller_sid = "S-1-5-21-111-222-333-1001"
    assert elevation._register_task_current_process(caller_sid) is True
    args = seen["args"]
    assert isinstance(args, list)
    text = str(args[-1])
    assert "-RunLevel Highest" in text
    assert "-LogonType Interactive" in text
    assert "--elevated-broker" in text
    assert "SetSecurityDescriptor" in text
    assert f"$callerSid = '{caller_sid}'" in text
    assert "FRFX;;;" in text
    assert "FA;;;' + $callerSid" not in text
    assert "FA;;;SY" in text
    assert "FA;;;BA" in text
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
    assert "--caller-sid" in command
    assert "WindowsIdentity]::GetCurrent().User.Value" in command
    assert "register-elevated-broker.ps1" not in command


def test_token_elevation_uses_pointer_sized_win32_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFn:
        def __init__(self, impl):
            self.impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.impl(*args)

    def get_current_process():
        return 0xFFFFFFFFFFFFFFFF

    def open_process_token(process, _access, handle_ptr):
        assert process == 0xFFFFFFFFFFFFFFFF
        elevation.ctypes.cast(
            handle_ptr, elevation.ctypes.POINTER(elevation.ctypes.c_void_p)
        ).contents.value = 123
        return 1

    def get_token_information(_handle, info_class, output, _size, size_ptr):
        assert info_class == 20
        elevation.ctypes.cast(
            output, elevation.ctypes.POINTER(elevation.ctypes.c_uint32)
        ).contents.value = 1
        elevation.ctypes.cast(
            size_ptr, elevation.ctypes.POINTER(elevation.ctypes.c_uint32)
        ).contents.value = 4
        return 1

    get_process = FakeFn(get_current_process)
    open_token = FakeFn(open_process_token)
    get_info = FakeFn(get_token_information)
    close_handle = FakeFn(lambda _handle: 1)
    fake_windll = SimpleNamespace(
        kernel32=SimpleNamespace(GetCurrentProcess=get_process, CloseHandle=close_handle),
        advapi32=SimpleNamespace(
            OpenProcessToken=open_token, GetTokenInformation=get_info
        ),
    )
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation.ctypes, "windll", fake_windll, raising=False)
    assert elevation._token_is_elevated() is True
    assert get_process.restype is elevation.ctypes.c_void_p
    assert open_token.argtypes is not None
    assert get_info.argtypes is not None


def test_registered_task_must_match_current_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "_task_exists", lambda: True)
    monkeypatch.setattr(
        elevation,
        "_broker_command",
        lambda: (r"C:\App\MCPDevBridge.exe", "--elevated-broker", r"C:\App"),
    )

    class Result:
        returncode = 0
        stdout = elevation.json.dumps(
            {
                "execute": r"C:\App\MCPDevBridge.exe",
                "arguments": "--elevated-broker",
                "working_directory": r"C:\App",
            }
        )

    monkeypatch.setattr(elevation.subprocess, "run", lambda *_args, **_kwargs: Result())
    assert elevation._task_matches_current_command() is True


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
