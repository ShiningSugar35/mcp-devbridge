"""Windows high-integrity broker for MCP DevBridge full-system mode.

The desktop stays ``asInvoker``. A full-system user authorizes one Task
Scheduler entry through normal Windows UAC. Later starts use that pre-authorized
highest-token task and a loopback-only authenticated broker. This module never
bypasses or disables UAC.
"""

from __future__ import annotations

import contextlib
import ctypes
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from . import constants
from .engines import CodexProManager, EngineState, SpawnError
from .execution_profile import check_execution
from .platform_support import IS_WINDOWS, run_platform_kwargs
from .secrets import generate_token, get_store
from .shell import run_command, run_program

BROKER_TASK_NAME = "MCP DevBridge Elevated Broker"
BROKER_STATE_FILE = "elevated-broker.json"
BROKER_MAX_BODY_BYTES = 1_048_576
BROKER_MAX_OUTPUT_CHARS = 262_144
BROKER_IDLE_SECONDS = 600.0
BROKER_REQUEST_TIMEOUT_SECONDS = 120.0
BROKER_START_TIMEOUT_SECONDS = 25.0
TASK_VALIDATION_TTL_SECONDS = 5.0


def _auth_store_key() -> str:
    return "MCPDevBridge:elevated-" + "auth"


def _state_path() -> Path:
    constants.ensure_dirs()
    return constants.config_dir() / BROKER_STATE_FILE


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _broker_command() -> tuple[str, str, str]:
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return exe, "--elevated-broker", str(Path(exe).parent)
    exe = str(Path(sys.executable).resolve())
    return (
        exe,
        "-m local_dev_mcp_bridge.elevation --broker",
        str(Path(__file__).resolve().parents[2]),
    )


def _task_exists() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", BROKER_TASK_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _normalized_windows_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip().strip('"')))


def _task_matches_current_command() -> bool:
    """Verify the registered task still targets this installed/runtime build."""
    if not IS_WINDOWS or not _task_exists():
        return False
    exe, args, workdir = _broker_command()
    script = f"""$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {_ps_quote(BROKER_TASK_NAME)} -TaskPath '\\'
$action = @($task.Actions)[0]
[pscustomobject]@{{
  execute = [string]$action.Execute
  arguments = [string]$action.Arguments
  working_directory = [string]$action.WorkingDirectory
}} | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout.strip() or "{}")
        return bool(
            isinstance(payload, dict)
            and _normalized_windows_path(str(payload.get("execute") or ""))
            == _normalized_windows_path(exe)
            and str(payload.get("arguments") or "").strip() == args.strip()
            and _normalized_windows_path(str(payload.get("working_directory") or ""))
            == _normalized_windows_path(workdir)
        )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return False


def _register_task_current_process(caller_sid: str = "") -> bool:
    """Register the highest-token task from an already elevated process.

    The standard-user desktop never executes a writable temporary script under
    UAC. It elevates the currently running executable into a dedicated
    registration entry point; only that elevated process creates the task.

    The task DACL deliberately gives the original medium-integrity caller only
    FILE_GENERIC_READ + FILE_GENERIC_EXECUTE. This lets the desktop query/run
    the pre-authorized task without allowing a medium process to rewrite the
    elevated action into an arbitrary privilege-escalation trampoline.
    """
    if not IS_WINDOWS or not _token_is_elevated():
        return False
    exe, args, workdir = _broker_command()
    safe_sid = caller_sid.strip()
    sid_expr = _ps_quote(safe_sid) if safe_sid else "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    script = f"""$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$callerSid = {sid_expr}
$action = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(args)} -WorkingDirectory {_ps_quote(workdir)}
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName {_ps_quote(BROKER_TASK_NAME)} -Action $action -Principal $principal -Settings $settings -Description 'MCP DevBridge local high-integrity broker; created after explicit UAC consent.' -Force | Out-Null
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$registered = $service.GetFolder('\\').GetTask({_ps_quote(BROKER_TASK_NAME)})
$sddl = 'D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;' + $callerSid + ')'
$registered.SetSecurityDescriptor($sddl, 0)
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0 and _task_exists()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _registration_command() -> tuple[str, list[str], str]:
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return exe, ["--register-elevated-broker-task"], str(Path(exe).parent)
    exe = str(Path(sys.executable).resolve())
    return (
        exe,
        ["-m", "local_dev_mcp_bridge.elevation", "--register-task"],
        str(Path(__file__).resolve().parents[2]),
    )


def _run_registration_uac() -> bool:
    if not IS_WINDOWS:
        return False
    exe, args, workdir = _registration_command()
    ps_args = ",".join(_ps_quote(arg) for arg in args)
    command = (
        "$callerSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$p=Start-Process -FilePath "
        + _ps_quote(exe)
        + " -Verb RunAs -Wait -PassThru -WorkingDirectory "
        + _ps_quote(workdir)
        + " -ArgumentList @("
        + ps_args
        + ",'--caller-sid',$callerSid); exit $p.ExitCode"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0 and _task_exists()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_task() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", BROKER_TASK_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _token_is_elevated() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        open_process_token = advapi32.OpenProcessToken
        open_process_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        open_process_token.restype = ctypes.c_int
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_token_information.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        handle = ctypes.c_void_p()
        process = get_current_process()
        if not open_process_token(process, 0x0008, ctypes.byref(handle)):
            return False
        try:
            elevated = ctypes.c_uint32(0)
            size = ctypes.c_uint32(0)
            ok = get_token_information(
                handle,
                20,  # TokenElevation
                ctypes.cast(ctypes.byref(elevated), ctypes.c_void_p),
                ctypes.sizeof(elevated),
                ctypes.byref(size),
            )
            return bool(ok and elevated.value)
        finally:
            close_handle(handle)
    except Exception:
        return False


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_state(port: int, epoch: str) -> None:
    path = _state_path()
    payload = {
        "schema": 1,
        "pid": os.getpid(),
        "port": port,
        "epoch": epoch,
        "elevated": _token_is_elevated(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _clear_state_if_ours(epoch: str) -> None:
    try:
        if _read_state().get("epoch") == epoch:
            _state_path().unlink(missing_ok=True)
    except OSError:
        pass


def _bounded_text(value: Any, limit: int = BROKER_MAX_OUTPUT_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class _BrokerRuntime:
    def __init__(self, auth_value: str) -> None:
        self.auth_value = auth_value
        self.children: dict[str, CodexProManager] = {}
        self.lock = threading.RLock()
        self.last_activity = time.monotonic()
        self.shutdown_requested = threading.Event()
        self._job: Any | None = None
        self._init_kill_on_close_job()

    def _init_kill_on_close_job(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            import win32job  # type: ignore[import-not-found]

            job: Any = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            self._job = job
        except Exception:
            self._job = None

    def _assign_pid_to_job(self, pid: int) -> None:
        if self._job is None or not IS_WINDOWS:
            return
        try:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32job  # type: ignore[import-not-found]

            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid
            )
            try:
                win32job.AssignProcessToJobObject(self._job, handle)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            pass

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def has_running_children(self) -> bool:
        with self.lock:
            return any(manager.is_running for manager in self.children.values())

    def running_child_count(self) -> int:
        with self.lock:
            return sum(1 for manager in self.children.values() if manager.is_running)

    def spawn_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        root = str(payload.get("root") or "").strip()
        access_value = str(payload.get("access_value") or "")
        bridge_value = str(payload.get("bridge_value") or "") or None
        port = int(payload.get("port") or 0)
        if not project_id or len(project_id) > 128:
            raise ValueError("invalid project_id")
        if not root or not Path(root).expanduser().is_dir():
            raise ValueError("root is not an existing directory")
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if len(access_value) < 24:
            raise ValueError("CodexPro access value is too short")
        extra_raw = payload.get("extra_env") or {}
        if not isinstance(extra_raw, dict):
            raise ValueError("extra_env must be an object")
        extra_env = {
            str(k): str(v)
            for k, v in extra_raw.items()
            if str(k) == "CODEXPRO_WINDOWS_BRIDGE_URL" and len(str(v)) <= 2048
        }
        with self.lock:
            current = self.children.get(project_id)
            if current is not None and current.is_running:
                return {
                    "ok": True,
                    "pid": current.pid,
                    "state": current.state.value,
                    "elevated": _token_is_elevated(),
                }
            if current is not None:
                with contextlib.suppress(Exception):
                    current.stop()
            manager = CodexProManager(
                log_dir=constants.process_log_dir() / project_id,
                port=port,
            )
            manager.start(
                root,
                access_value,
                permission_mode="system",
                windows_token=bridge_value,
                execution_profile="full_system",
                extra_env=extra_env,
            )
            if not manager.wait_ready():
                raise SpawnError(manager.error or "elevated CodexPro did not become ready")
            self.children[project_id] = manager
            pid = manager.pid or 0
            if pid:
                self._assign_pid_to_job(pid)
            self.touch()
            return {
                "ok": True,
                "pid": pid,
                "state": manager.state.value,
                "elevated": _token_is_elevated(),
            }

    def child_status(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            manager = self.children.get(project_id)
            if manager is None:
                return {
                    "ok": True,
                    "exists": False,
                    "running": False,
                    "state": EngineState.IDLE.value,
                }
            return {
                "ok": True,
                "exists": True,
                "running": manager.is_running,
                "state": manager.state.value,
                "pid": manager.pid,
                "error": manager.error or "",
            }

    def stop_child(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            manager = self.children.pop(project_id, None)
        if manager is not None:
            manager.stop()
        self.touch()
        return {"ok": True}

    def log_tail(self, project_id: str, count: int) -> dict[str, Any]:
        with self.lock:
            manager = self.children.get(project_id)
            text = manager.log_tail(max(1, min(count, 400))) if manager else "(尚无输出)"
        return {"ok": True, "text": _bounded_text(text)}

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "command")
        cwd_raw = str(payload.get("cwd") or "").strip()
        cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else Path.cwd()
        if not cwd.is_dir():
            raise ValueError(f"cwd 不存在或不是目录：{cwd}")
        timeout = max(1, min(int(payload.get("timeout_seconds") or 10), 20))
        if kind == "command":
            command = str(payload.get("command") or "")
            allowed, reason = check_execution(command, "full_system")
            if not allowed:
                raise ValueError(reason)
            result = run_command(command, cwd=cwd, timeout_seconds=timeout)
            return {
                "ok": True,
                "shell": result.shell,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "stdout": _bounded_text(result.stdout),
                "stderr": _bounded_text(result.stderr),
            }
        if kind == "program":
            executable = str(payload.get("executable") or "")
            args = [str(item) for item in (payload.get("args") or [])]
            if len(args) > 256 or any(len(arg) > 16_384 for arg in args):
                raise ValueError("program arguments exceed broker limits")
            allowed, reason = check_execution(" ".join([executable, *args]), "full_system")
            if not allowed:
                raise ValueError(reason)
            result = run_program(executable, args, cwd=cwd, timeout_seconds=timeout)
            return {
                "ok": True,
                "command": _bounded_text(result.command, 32_768),
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "stdout": _bounded_text(result.stdout),
                "stderr": _bounded_text(result.stderr),
            }
        raise ValueError("unsupported execution kind")

    def stop_all(self) -> None:
        with self.lock:
            managers = list(self.children.values())
            self.children.clear()
        for manager in managers:
            with contextlib.suppress(Exception):
                manager.stop()


class _BrokerHandler(BaseHTTPRequestHandler):
    server_version = "MCPDevBridgeElevated/1"

    @property
    def runtime(self) -> _BrokerRuntime:
        return self.server.runtime  # type: ignore[no-any-return]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        del format, args
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if str(self.client_address[0]) not in {"127.0.0.1", "::1"}:
            self._json(403, {"ok": False, "error": "loopback only"})
            return
        supplied = self.headers.get("X-MCPDB-Auth", "")
        if not hmac.compare_digest(supplied, self.runtime.auth_value):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > BROKER_MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "request too large"})
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            action = str(payload.get("action") or "")
            self.runtime.touch()
            if action == "health":
                result = {
                    "ok": True,
                    "pid": os.getpid(),
                    "elevated": _token_is_elevated(),
                    "children": self.runtime.running_child_count(),
                }
            elif action == "spawn_codex":
                result = self.runtime.spawn_codex(payload)
            elif action == "child_status":
                result = self.runtime.child_status(str(payload.get("project_id") or ""))
            elif action == "stop_child":
                result = self.runtime.stop_child(str(payload.get("project_id") or ""))
            elif action == "log_tail":
                result = self.runtime.log_tail(
                    str(payload.get("project_id") or ""), int(payload.get("count") or 200)
                )
            elif action == "execute":
                result = self.runtime.execute(payload)
            elif action == "shutdown_if_idle":
                if self.runtime.has_running_children():
                    result = {"ok": False, "busy": True}
                else:
                    self.runtime.shutdown_requested.set()
                    result = {"ok": True, "shutting_down": True}
            else:
                raise ValueError("unsupported broker action")
            self._json(200, result)
        except (ValueError, SpawnError) as exc:
            self._json(400, {"ok": False, "error": _bounded_text(exc, 2048)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": type(exc).__name__})


@dataclass
class ElevatedCommandResult:
    exit_code: int
    duration_seconds: float
    timed_out: bool
    stdout: str
    stderr: str
    shell: str = ""
    command: str = ""


class ElevationController:
    def __init__(self) -> None:
        self._auth_cache = ""
        self._lock = threading.RLock()
        self._registration_cache_until = 0.0
        self._registration_cache_value = False

    def is_registered(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now < self._registration_cache_until:
                return self._registration_cache_value
            value = _task_matches_current_command()
            self._registration_cache_value = value
            self._registration_cache_until = now + TASK_VALIDATION_TTL_SECONDS
            return value

    def _cache_registration(self, value: bool) -> bool:
        with self._lock:
            self._registration_cache_value = value
            self._registration_cache_until = time.monotonic() + TASK_VALIDATION_TTL_SECONDS
        return value

    def _auth_value(self) -> str:
        with self._lock:
            if self._auth_cache:
                return self._auth_cache
            store = get_store()
            value = store.get(_auth_store_key())
            if not value or len(value) < 32:
                value = generate_token(256)
                store.set(_auth_store_key(), value)
            self._auth_cache = value
            return value

    def ensure_registered(self, *, interactive: bool) -> bool:
        if not IS_WINDOWS:
            return False
        if self.is_registered():
            return True
        self._auth_value()
        if _token_is_elevated():
            return self._cache_registration(_register_task_current_process())
        if not interactive:
            return False
        return self._cache_registration(_run_registration_uac())

    def _request_raw(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = BROKER_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        state = _read_state()
        try:
            port = int(state.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not port:
            raise RuntimeError("elevated broker is not running")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib_request.Request(
            f"http://127.0.0.1:{port}/rpc",
            method="POST",
            data=body,
            headers={
                "X-MCPDB-Auth": self._auth_value(),
                "Content-Type": "application/json",
                "User-Agent": "MCPDevBridge-elevation-client",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read(BROKER_MAX_BODY_BYTES).decode("utf-8"))
        except urllib_error.HTTPError as exc:
            raw = exc.read(8192).decode("utf-8", errors="replace")
            raise RuntimeError(f"elevated broker HTTP {exc.code}: {raw}") from None
        except (OSError, TimeoutError, urllib_error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"elevated broker unavailable: {type(exc).__name__}") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            detail = result.get("error") if isinstance(result, dict) else "invalid reply"
            raise RuntimeError(str(detail or "broker request failed"))
        return result

    def health(self) -> dict[str, Any] | None:
        try:
            return self._request_raw({"action": "health"}, timeout=2.0)
        except RuntimeError:
            return None

    def ensure_running(self, *, interactive_registration: bool = False) -> dict[str, Any]:
        # Serialize startup across "start all" workers. Without this lock one
        # worker can delete the fresh state file written by another worker after
        # Task Scheduler has already accepted the first broker instance.
        with self._lock:
            current = self.health()
            if current and current.get("elevated") is True:
                return current
            if not self.ensure_registered(interactive=interactive_registration):
                raise RuntimeError(
                    "Windows 管理员执行能力尚未授权。请首次启用「完全访问」并完成一次 UAC 授权。"
                )
            with contextlib.suppress(OSError):
                _state_path().unlink(missing_ok=True)
            if not _run_task():
                raise RuntimeError("无法启动已注册的 Windows 高权限 broker 任务。")
            deadline = time.monotonic() + BROKER_START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                health = self.health()
                if health:
                    if health.get("elevated") is not True:
                        raise RuntimeError(
                            "高权限 broker 已启动，但 Windows token 未处于 elevated 状态。"
                        )
                    return health
                time.sleep(0.2)
            raise RuntimeError("Windows 高权限 broker 启动超时。")

    def spawn_codex(
        self,
        *,
        project_id: str,
        root: str,
        port: int,
        access_value: str,
        bridge_value: str | None,
        extra_env: dict[str, str] | None,
    ) -> dict[str, Any]:
        self.ensure_running(interactive_registration=False)
        return self._request_raw(
            {
                "action": "spawn_codex",
                "project_id": project_id,
                "root": root,
                "port": port,
                "access_value": access_value,
                "bridge_value": bridge_value or "",
                "extra_env": extra_env or {},
            }
        )

    def child_status(self, project_id: str) -> dict[str, Any]:
        return self._request_raw({"action": "child_status", "project_id": project_id}, timeout=3.0)

    def stop_child(self, project_id: str) -> None:
        last_error: RuntimeError | None = None
        for attempt in range(2):
            try:
                self._request_raw({"action": "stop_child", "project_id": project_id}, timeout=12.0)
                return
            except RuntimeError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.1)
        assert last_error is not None
        raise last_error

    def log_tail(self, project_id: str, count: int = 200) -> str:
        try:
            result = self._request_raw(
                {"action": "log_tail", "project_id": project_id, "count": count},
                timeout=3.0,
            )
            return str(result.get("text") or "")
        except RuntimeError:
            return "(高权限 broker 当前不可用)"

    def execute_command(
        self, command: str, cwd: Path, timeout_seconds: int
    ) -> ElevatedCommandResult:
        self.ensure_running(interactive_registration=False)
        result = self._request_raw(
            {
                "action": "execute",
                "kind": "command",
                "command": command,
                "cwd": str(cwd),
                "timeout_seconds": timeout_seconds,
            },
            timeout=max(25.0, float(timeout_seconds) + 5.0),
        )
        return ElevatedCommandResult(
            exit_code=int(result.get("exit_code") or 0),
            duration_seconds=float(result.get("duration_seconds") or 0.0),
            timed_out=bool(result.get("timed_out")),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            shell=str(result.get("shell") or ""),
        )

    def execute_program(
        self,
        executable: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> ElevatedCommandResult:
        self.ensure_running(interactive_registration=False)
        result = self._request_raw(
            {
                "action": "execute",
                "kind": "program",
                "executable": executable,
                "args": args,
                "cwd": str(cwd),
                "timeout_seconds": timeout_seconds,
            },
            timeout=max(25.0, float(timeout_seconds) + 5.0),
        )
        return ElevatedCommandResult(
            exit_code=int(result.get("exit_code") or 0),
            duration_seconds=float(result.get("duration_seconds") or 0.0),
            timed_out=bool(result.get("timed_out")),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            command=str(result.get("command") or ""),
        )

    def shutdown_if_idle(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._request_raw({"action": "shutdown_if_idle"}, timeout=3.0)


class ElevatedCodexProManager:
    """CodexPro facade whose child process is owned by the elevated broker."""

    def __init__(self, project_id: str, *, log_dir: Path, port: int) -> None:
        self.project_id = project_id
        self.log_dir = log_dir
        self.port = port
        self._controller = get_elevation_controller()
        self._state = EngineState.IDLE
        self._error: str | None = None
        self._pid: int | None = None
        self._status_deadline = 0.0

    @property
    def state(self) -> EngineState:
        if (
            self._state in {EngineState.STARTING, EngineState.READY}
            and time.monotonic() >= self._status_deadline
        ):
            try:
                status = self._controller.child_status(self.project_id)
                if not status.get("running"):
                    self._state = EngineState.ERROR
                    self._error = str(status.get("error") or "高权限 CodexPro 进程已退出。")
                elif self._state == EngineState.STARTING:
                    self._state = EngineState.READY
                self._status_deadline = time.monotonic() + 0.25
            except RuntimeError as exc:
                self._state = EngineState.ERROR
                self._error = str(exc)
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def is_running(self) -> bool:
        return self.state == EngineState.READY

    def start(
        self,
        root: str,
        access_value: str,
        *,
        permission_mode: str = "system",
        windows_token: str | None = None,
        execution_profile: str = "full_system",
        extra_env: dict[str, str] | None = None,
    ) -> None:
        del permission_mode, execution_profile
        if self.is_running:
            return
        self._state = EngineState.STARTING
        self._status_deadline = 0.0
        try:
            result = self._controller.spawn_codex(
                project_id=self.project_id,
                root=root,
                port=self.port,
                access_value=access_value,
                bridge_value=windows_token,
                extra_env=extra_env,
            )
            self._pid = int(result.get("pid") or 0) or None
            if result.get("elevated") is not True:
                raise SpawnError("CodexPro broker process is not elevated")
            self._state = EngineState.READY
            self._error = None
            self._status_deadline = time.monotonic() + 0.25
        except Exception as exc:
            self._state = EngineState.ERROR
            self._error = str(exc)
            raise SpawnError(str(exc)) from exc

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        return self.state == EngineState.READY

    def stop(self, timeout_seconds: float = 8.0) -> None:
        del timeout_seconds
        self._state = EngineState.STOPPING
        try:
            self._controller.stop_child(self.project_id)
        except RuntimeError as exc:
            self._state = EngineState.ERROR
            self._error = f"停止高权限 CodexPro 失败：{exc}"
            raise SpawnError(self._error) from exc
        self._state = EngineState.IDLE
        self._error = None
        self._pid = None
        self._status_deadline = 0.0

    def log_tail(self, count: int = 200) -> str:
        return self._controller.log_tail(self.project_id, count)


_CONTROLLER: ElevationController | None = None
_CONTROLLER_LOCK = threading.Lock()


def get_elevation_controller() -> ElevationController:
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = ElevationController()
        return _CONTROLLER


def broker_main() -> int:
    if not IS_WINDOWS:
        return 2
    if not _token_is_elevated():
        return 3
    auth_value = get_store().get(_auth_store_key())
    if not auth_value or len(auth_value) < 32:
        return 4
    runtime = _BrokerRuntime(auth_value)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokerHandler)
    server_ext: Any = server
    server_ext.runtime = runtime
    server.timeout = 0.5
    epoch = f"{os.getpid():x}-{time.time_ns():x}"
    _write_state(int(server.server_address[1]), epoch)
    try:
        while not runtime.shutdown_requested.is_set():
            server.handle_request()
            if (
                not runtime.has_running_children()
                and time.monotonic() - runtime.last_activity >= BROKER_IDLE_SECONDS
            ):
                break
    finally:
        runtime.stop_all()
        server.server_close()
        _clear_state_if_ours(epoch)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--register-task" in args or "--register-elevated-broker-task" in args:
        caller_sid = ""
        if "--caller-sid" in args:
            index = args.index("--caller-sid")
            if index + 1 < len(args):
                caller_sid = args[index + 1]
        return 0 if _register_task_current_process(caller_sid) else 5
    if "--broker" in args or "--elevated-broker" in args:
        return broker_main()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BROKER_TASK_NAME",
    "ElevationController",
    "ElevatedCodexProManager",
    "ElevatedCommandResult",
    "get_elevation_controller",
    "broker_main",
]
