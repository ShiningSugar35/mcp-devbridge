"""Regression checks for the portable Windows startup path."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import sysconfig
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from local_dev_mcp_bridge import live_bridge_worker

_ADMIN_ERROR = "完全访问需要先完成 DevBridge 的 Windows 管理员登记。"


def test_worker_error_protocol_does_not_use_windows_console_encoding(tmp_path, monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(output, encoding="cp936"))
    request = {"projects": [{"name": "fixture", "root": str(tmp_path), "mode": "system"}],
               "gateway_token": "fixture-token"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(live_bridge_worker, "ProjectManager", lambda: _StubManager())
    monkeypatch.setattr(live_bridge_worker.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(live_bridge_worker, "_ports", lambda count: list(range(40001, 40001 + count)))
    monkeypatch.setattr(live_bridge_worker, "save_projects", lambda projects: None)

    def fail_admin(modes):
        raise RuntimeError(_ADMIN_ERROR)

    monkeypatch.setattr(live_bridge_worker, "_startup_elevation", fail_admin)
    assert live_bridge_worker.main() == 1
    assert json.loads(output.getvalue().decode("utf-8")) == {"error": _ADMIN_ERROR}


class _StubManager:
    def stop_all(self):
        pass


def test_windows_authorization_only_for_system_projects(monkeypatch):
    monkeypatch.setattr(live_bridge_worker, "IS_WINDOWS", True)
    calls = []
    monkeypatch.setattr(live_bridge_worker, "_live_admin_session", lambda: calls.append("uac") or nullcontext())

    with live_bridge_worker._startup_elevation(["read_only", "workspace"]):
        assert calls == []
    with live_bridge_worker._startup_elevation(["workspace", "system", "system"]):
        assert calls == ["uac"]

    monkeypatch.setattr(live_bridge_worker, "IS_WINDOWS", False)
    with live_bridge_worker._startup_elevation(["system"]):
        assert calls == ["uac"]


def test_broker_uac_cancellation_is_reported_in_chinese(monkeypatch):
    from local_dev_mcp_bridge import live_elevation

    class UacCancelled(OSError):
        winerror = 1223

    def denied(*args, **kwargs):
        raise UacCancelled(1223, "canceled")

    monkeypatch.setattr(live_elevation, "_exec_runas", denied)
    with pytest.raises(RuntimeError, match="已取消 Windows 管理员授权"):
        live_elevation._launch_broker("test-pipe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")
def test_native_pipe_handoff_checks_process_and_delivers_secret():
    import secrets

    import win32api  # type: ignore[import-not-found]

    from local_dev_mcp_bridge import live_elevation

    name = live_elevation._PIPE_PREFIX + secrets.token_hex(16)
    auth = "f" * 64
    package = str(Path(live_elevation.__file__).resolve().parents[1])
    code = (
        f"import site,sys;site.addsitedir({sysconfig.get_path('purelib')!r});"
        f"sys.path.insert(0,{package!r});"
        "import win32con,win32file;"
        "from local_dev_mcp_bridge import live_elevation as l;"
        "p=win32file.CreateFile(sys.argv[1],win32con.GENERIC_READ|win32con.GENERIC_WRITE,"
        "0,None,win32con.OPEN_EXISTING,l._SECURITY_SQOS_PRESENT|win32file.SECURITY_IDENTIFICATION,None);"
        "assert l._peer_pid(p,client=False)==int(sys.argv[2]);"
        "assert win32file.ReadFile(p,64)[1]==b'f'*64;"
        "win32file.WriteFile(p,b'48573');"
        "import time;time.sleep(2);p.Close()"
    )
    pipe = live_elevation._create_pipe(name)
    try:
        python = getattr(sys, "_base_executable", sys.executable)
        with subprocess.Popen([python, "-I", "-B", "-c", code, name, str(os.getpid())]) as child:
            process: Any = win32api.OpenProcess(0x00101000, False, child.pid)  # synchronize + query
            try:
                assert live_elevation._await_port(pipe, process, auth) == 48573
            finally:
                process.Close()
            assert child.wait(timeout=6) == 0
    finally:
        pipe.Close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UAC")
def test_elevated_host_runs_a_real_live_broker():
    from local_dev_mcp_bridge import elevation, live_elevation

    if not elevation._token_is_elevated():
        pytest.skip("requires a Windows test worker with an administrator token")
    original = elevation.get_elevation_controller()
    with live_elevation.live_admin_session():
        controller = elevation.get_elevation_controller()
        assert controller is not original
        assert controller.ensure_running()["elevated"] is True
    assert elevation.get_elevation_controller() is original
