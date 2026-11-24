"""Session-scoped Windows UAC for the portable Live bridge."""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import secrets
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib import request as urllib_request

from . import elevation
from .platform_support import IS_WINDOWS

_PIPE_PREFIX = r"\\.\pipe\MCP-Live-"
_CANCELLED = 1223
_PIPE_TIMEOUT = 25.0
_FIRST_PIPE_INSTANCE = 0x00080000
_SECURITY_SQOS_PRESENT = 0x00100000


def _peer_pid(pipe: Any, *, client: bool) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    query = kernel.GetNamedPipeClientProcessId if client else kernel.GetNamedPipeServerProcessId
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    query.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not query(int(pipe), ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore[reportAttributeAccessIssue]
    return pid.value


def _exec_runas(executable: str, arguments: list[str]) -> Any:
    import pywintypes  # type: ignore[import-not-found]
    from win32com.shell import shell, shellcon  # type: ignore[import-not-found]

    try:
        result = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=executable,
            lpParameters=subprocess.list2cmdline(arguments),
            lpDirectory=str(Path(executable).parent),
            nShow=0,
        )
    except pywintypes.error as exc:
        raise ctypes.WinError(exc.winerror) from exc  # pyright: ignore[reportAttributeAccessIssue]
    process = result.get("hProcess")
    if not process:
        raise RuntimeError("Windows 未返回管理员进程。")
    return process


def _launch_broker(pipe_name: str) -> Any:
    arguments = [pipe_name, str(os.getpid())]
    if getattr(sys, "frozen", False):
        executable = sys.executable
        args = ["--live-elevated-broker", *arguments]
    else:
        # Use the real interpreter so the pipe client PID also matches for a venv.
        executable = getattr(sys, "_base_executable", sys.executable)
        source = str(Path(__file__).resolve().parents[1])
        packages = sysconfig.get_path("purelib")
        bootstrap = (
            f"import site,sys;site.addsitedir({packages!r});sys.path.insert(0,{source!r});"
            "from local_dev_mcp_bridge.live_elevation import broker_main;"
            "raise SystemExit(broker_main(sys.argv[1:]))"
        )
        args = ["-I", "-B", "-c", bootstrap, *arguments]
    try:
        return _exec_runas(executable, args)
    except OSError as exc:
        if getattr(exc, "winerror", None) == _CANCELLED:
            raise RuntimeError("已取消 Windows 管理员授权。完全访问未启动。") from None
        raise RuntimeError(f"无法启动 Windows 管理员进程（{exc}）。") from None


def _create_pipe(name: str) -> Any:
    import pywintypes  # type: ignore[import-not-found]
    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32pipe  # type: ignore[import-not-found]
    import win32security  # type: ignore[import-not-found]

    token: Any = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        token.Close()
    caller = win32security.ConvertSidToStringSid(sid)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{caller})", 1,
    )
    mode = win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT
    return win32pipe.CreateNamedPipe(
        name, win32pipe.PIPE_ACCESS_DUPLEX | _FIRST_PIPE_INSTANCE,
        mode | win32pipe.PIPE_REJECT_REMOTE_CLIENTS, 1, 4096, 4096, 0, attributes,
    )


def _await_port(pipe: Any, process: Any, auth: str) -> int:
    import pywintypes  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]
    import win32file  # type: ignore[import-not-found]
    import win32pipe  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]
    import winerror  # type: ignore[import-not-found]

    expected_pid = win32process.GetProcessId(process)
    deadline = time.monotonic() + _PIPE_TIMEOUT
    connected = False
    while time.monotonic() < deadline:
        if win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0:
            code = win32process.GetExitCodeProcess(process)
            raise RuntimeError(f"Windows 管理员进程提前退出（退出码 {code}）。")
        if not connected:
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
            except pywintypes.error as exc:
                if exc.winerror == winerror.ERROR_PIPE_LISTENING:
                    time.sleep(0.05)
                    continue
                if exc.winerror == winerror.ERROR_NO_DATA:
                    win32pipe.DisconnectNamedPipe(pipe)
                    continue
                if exc.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            if _peer_pid(pipe, client=True) != expected_pid:
                win32pipe.DisconnectNamedPipe(pipe)
                continue
            win32file.WriteFile(pipe, auth.encode("ascii"))
            connected = True
        if win32pipe.PeekNamedPipe(pipe, 0)[1]:
            _, data = win32file.ReadFile(pipe, 32)
            if data.isdigit() and 0 < int(data) < 65536:
                return int(data)
            raise RuntimeError("Windows 管理员进程返回了无效的本机端口。")
        time.sleep(0.05)
    raise RuntimeError("Windows 管理员进程未在限定时间内就绪。")


class _LiveController(elevation.ElevationController):
    def __init__(self, port: int, auth: str) -> None:
        super().__init__()
        self._port = port
        self._auth = auth
        self._opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))

    def _broker_port(self) -> int:
        return self._port

    def _auth_value(self) -> str:
        return self._auth

    def _open_request(self, request: urllib_request.Request, timeout: float) -> Any:
        return self._opener.open(request, timeout=timeout)

    def is_registered(self) -> bool:
        status = self.health()
        return status is not None and status.get("elevated") is True

    def ensure_running(self, *, interactive_registration: bool = False) -> dict[str, Any]:
        status = self.health()
        if status is None or status.get("elevated") is not True:
            raise RuntimeError("本次共享的 Windows 管理员进程不可用。请重新启动共享并授权。")
        return status


@contextlib.contextmanager
def live_admin_session() -> Iterator[None]:
    import pywintypes  # type: ignore[import-not-found]

    name = _PIPE_PREFIX + secrets.token_hex(16)
    auth = secrets.token_hex(32)
    pipe = None
    process = None
    try:
        try:
            pipe = _create_pipe(name)
            process = _launch_broker(name)
            port = _await_port(pipe, process, auth)
        except (pywintypes.error, OSError) as exc:
            raise RuntimeError(f"无法建立 Windows 管理员会话（{exc}）。") from None
        controller = _LiveController(port, auth)
        controller.ensure_running()
        with elevation._use_elevation_controller(controller):
            yield
    finally:
        # Disconnecting this private pipe also shuts down the elevated broker.
        if pipe is not None:
            pipe.Close()
        if process is not None:
            process.Close()


def _watch_pipe(pipe: Any, stopped: threading.Event) -> None:
    import pywintypes  # type: ignore[import-not-found]
    import win32file  # type: ignore[import-not-found]

    try:
        win32file.ReadFile(pipe, 1)
    except pywintypes.error:
        pass
    finally:
        stopped.set()


def _serve_session(pipe: Any, auth: str) -> None:
    import win32file  # type: ignore[import-not-found]

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        node = root / "runtime" / "node.exe"
        dist = root / "third_party" / "codexpro" / "dist"
        if not node.is_file() or not (dist / "http.js").is_file():
            raise RuntimeError("Missing packaged Node/CodexPro runtime.")
        os.environ["MCPDEVBRIDGE_NODE_EXE"] = str(node)
        os.environ["CODEXPRO_DIST_DIR"] = str(dist)
        os.environ.pop("NODE_OPTIONS", None)
        os.environ.pop("NODE_PATH", None)
    with TemporaryDirectory(prefix="mcp-live-admin-") as directory:
        os.environ["LOCALDEV_MCP_CONFIG_DIR"] = directory
        runtime = elevation._BrokerRuntime(auth)
        with elevation._broker_server(runtime) as server:
            try:
                win32file.WriteFile(pipe, str(server.server_address[1]).encode("ascii"))
                threading.Thread(
                    target=_watch_pipe, args=(pipe, runtime.shutdown_requested), daemon=True,
                ).start()
                while not runtime.shutdown_requested.is_set():
                    server.handle_request()
            finally:
                runtime.stop_all()


def broker_main(args: list[str]) -> int:
    if not IS_WINDOWS or not elevation._token_is_elevated():
        return 3
    if len(args) != 2 or not re.fullmatch(re.escape(_PIPE_PREFIX) + r"[0-9a-f]{32}", args[0]):
        return 2
    if not re.fullmatch(r"[1-9][0-9]{0,9}", args[1]):
        return 2
    import pywintypes  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32file  # type: ignore[import-not-found]

    try:
        pipe: Any = win32file.CreateFile(
            args[0], win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0,
            None, win32con.OPEN_EXISTING, _SECURITY_SQOS_PRESENT | win32file.SECURITY_IDENTIFICATION, None,
        )
        try:
            if _peer_pid(pipe, client=False) != int(args[1]):
                return 4
            message = cast(bytes, win32file.ReadFile(pipe, 64)[1])
            if not re.fullmatch(rb"[0-9a-f]{64}", message):
                return 4
            _serve_session(pipe, message.decode("ascii"))
        finally:
            pipe.Close()
    except (pywintypes.error, OSError, RuntimeError, ValueError):
        return 5
    return 0
