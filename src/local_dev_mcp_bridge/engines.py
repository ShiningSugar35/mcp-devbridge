"""CodexPro / Windows-MCP engine subprocess lifecycle (no UI, no Qt).

Pure-Python module so the state machine, command building, readiness parsing,
and secret handling are unit-testable without a display server.

Two engines are managed:

* ``CodexProManager`` - the default project engine. Runs
  ``third_party/codexpro``'s ``dist/http.js`` on a FIXED internal port
  (``CODEXPRO_LOCAL_PORT``) bound to 127.0.0.1.
* ``WindowsBridgeManager`` (optional) - the uvx-run Windows-MCP bridge on
  another FIXED internal port (``WINDOWS_BRIDGE_PORT``). It only listens on
  loopback; the app never passes its URL to CodexPro (the fork's
  ``windowsBridge.ts`` uses the fixed default port or its own env).

Secrets (codexpro HTTP token, windows bridge bearer) are never written to
logs: every captured line passes through :func:`redact_line` which masks the
exact secret values, and ``--auth-key`` style values are hidden in command
logs.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import constants

# 端口默认值集中维护（constants.DEFAULT_*_PORT）；下列为引擎层兼容别名。
CODEXPRO_LOCAL_PORT = constants.DEFAULT_CODEXPRO_PORT
WINDOWS_BRIDGE_PORT = constants.DEFAULT_WINDOWS_MCP_PORT
# 锁定 Windows-MCP 发布版本：上游迭代频繁（0.8.5 起 requires-python >=3.14），
# 白名单/协议均按锁定版本验证。升级必须通过兼容性测试后人工修改此常量。
WINDOWS_MCP_PINNED_VERSION = "0.8.2"
DEFAULT_ENGINE_START_TIMEOUT_SECONDS = 90
DEFAULT_WINDOWS_START_TIMEOUT_SECONDS = 240  # first uvx run may fetch the package
READY_POLL_INTERVAL_SECONDS = 0.15

CODEXPRO_READY_RE = re.compile(r"\[CodexPro\] HTTP MCP listening on http://127\.0\.0\.1:\d+/mcp")


def redact_line(line: str, secrets: tuple[str, ...] = ()) -> str:
    """Mask secret values and common sensitive marker words in a log line."""
    masked = line
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, "***")
    masked = re.sub(
        r"(?i)\b(?:key|token|secret|password|auth)(?:[_\-][a-z0-9_]+)?\s*=\s*\S+",
        lambda m: m.group(0).rsplit("=", 1)[0] + "=***",
        masked,
    )
    masked = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", masked)
    masked = re.sub(r"(?i)(--(?:auth-key|token|secret))\s+\S+", r"\1 ***", masked)
    return masked


def sanitize_cmd_for_log(cmd: list[str], secrets: tuple[str, ...] = ()) -> str:
    """Display a command with token-bearing flags masked."""
    secret_flags = {
        "--auth-key",
        "--token",
        "--secret",
        "WINDOWS_MCP_AUTH_KEY=",
        "CODEXPRO_WINDOWS_BRIDGE_TOKEN=",
        "CODEXPRO_HTTP_TOKEN=",
    }
    out: list[str] = []
    for i, part in enumerate(cmd):
        if part in secrets or part in secret_flags or i > 0 and cmd[i - 1] in ("--auth-key", "--token", "--secret"):
            out.append("***")
        else:
            out.append(part)
    return " ".join(out)


@dataclass
class ProcessLog:
    """Thread-safe ring buffer of recent output lines."""

    lines: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def tail(self, count: int = 200) -> str:
        with self.lock:
            return "".join(list(self.lines)[-count:])

    def clear(self) -> None:
        with self.lock:
            self.lines.clear()


class EngineState(StrEnum):
    """Service state labels used by the UI status chip and state machine."""

    IDLE = "未启动"
    STARTING = "启动中"
    READY = "已连接"
    STOPPING = "停止中"
    ERROR = "失败"


class SpawnError(RuntimeError):
    """Raised when an engine binary or artifact cannot be used."""


class BaseEngineProcess:
    """Small subprocess wrapper with a background line reader."""

    def __init__(
        self,
        cmd: list[str],
        env: dict[str, str] | None = None,
        secrets: tuple[str, ...] = (),
        cwd: Path | None = None,
        log_file: Path | None = None,
    ) -> None:
        self.cmd = cmd
        self.env = env
        self.secrets = secrets
        self.log = ProcessLog()
        effective_env = {**os.environ, **(env or {})}
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=effective_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
        self._file = log_file
        if self._file is not None:
            self._file.parent.mkdir(parents=True, exist_ok=True)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            for raw in iter(self._proc.stdout.readline, ""):
                safe = redact_line(raw.rstrip(), self.secrets)
                self.log.append(safe + "\n")
                if self._file is not None:
                    try:
                        with self._file.open("a", encoding="utf-8") as fh:
                            fh.write(safe + "\n")
                    except OSError:
                        pass
        except (ValueError, OSError):
            pass

    @property
    def is_running(self) -> bool:
        return self._proc.poll() is None

    @property
    def pid(self) -> int:
        return self._proc.pid

    def stop(self, timeout_seconds: float = 8.0) -> None:
        if self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout_seconds)
        except Exception:
            try:
                from .shell import kill_process_tree

                kill_process_tree(self._proc.pid)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """Cheap TCP probe used for readiness checks."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def wait_port(
    port: int,
    timeout_seconds: float = 30.0,
    interval: float = READY_POLL_INTERVAL_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if port_listening(port):
            return True
        time.sleep(interval)
    return False


def find_node() -> str:
    return shutil.which("node") or shutil.which("node.exe") or ""


def find_uvx() -> str:
    return shutil.which("uvx") or shutil.which("uvx.exe") or ""


def build_codex_env(
    root: str,
    *,
    permission_mode: str,
    token: str,
    windows_token: str | None = None,
    tool_mode: str = "standard",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for the CodexPro fork.

    The token travels only here; it is never written to logs or state files.
    """
    mode = (permission_mode or "workspace").lower()
    write_mode = {"read_only": "off", "workspace": "workspace", "system": "workspace"}.get(
        mode, "workspace"
    )
    if mode == "read_only":
        bash_mode, tool = "off", "minimal"
    elif mode == "system":
        bash_mode, tool = "full", "full"
    else:
        bash_mode, tool = "safe", tool_mode
    env: dict[str, str] = {
        "CODEXPRO_HTTP_TOKEN": token,
        "CODEXPRO_ROOT": root,
        "CODEXPRO_ALLOWED_ROOTS": root,
        "CODEXPRO_WRITE_MODE": write_mode,
        "CODEXPRO_BASH_MODE": bash_mode,
        "CODEXPRO_TOOL_MODE": tool,
        # Windows 桥接权限档位：完全访问 → 全部工具；其余 → desktop_ui 白名单。
        "CODEXPRO_WINDOWS_PROFILE": "system_full" if mode == "system" else "desktop_ui",
        "CODEXPRO_TUNNEL_MODE": "0",
        "CODEXPRO_HOST": "127.0.0.1",
    }
    if windows_token:
        env["CODEXPRO_WINDOWS_BRIDGE_TOKEN"] = windows_token
    if extra:
        env.update(extra)
    return env


def build_codex_cmd(node_exe: str, http_js: Path, root: str, port: int) -> list[str]:
    return [node_exe, str(http_js), "--root", root, "--port", str(port)]


class EngineManager:
    """Shared lifecycle for a managed engine subprocess."""

    def __init__(self, executable: str, label: str) -> None:
        self.executable = executable
        self.label = label
        self._proc: BaseEngineProcess | None = None
        self._state = EngineState.IDLE
        self._error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- state
    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    @property
    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.is_running

    def _set_state(self, state: EngineState, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._error = error

    # ------------------------------------------------------------- logs
    def log_tail(self, count: int = 200) -> str:
        proc = self._proc
        return proc.log.tail(count) if proc is not None else "(尚无输出)"

    # ------------------------------------------------------------- proc
    def _spawn(
        self,
        cmd: list[str],
        env: dict[str, str],
        secrets: tuple[str, ...],
        log_file: Path | None,
    ) -> BaseEngineProcess:
        proc = BaseEngineProcess(cmd, env=env, secrets=secrets, log_file=log_file)
        self._proc = proc
        return proc

    def stop(self, timeout_seconds: float = 8.0) -> None:
        self._set_state(EngineState.STOPPING)
        proc = self._proc
        if proc is not None:
            proc.stop(timeout_seconds=timeout_seconds)
        self._proc = None
        self._set_state(EngineState.IDLE)

    def _fail(self, message: str) -> None:
        self._set_state(EngineState.ERROR, message)
        proc = self._proc
        self._proc = None
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.stop(timeout_seconds=5)


class CodexProManager(EngineManager):
    """Owns the CodexPro HTTP engine subprocess (fixed local port."""

    def __init__(
        self,
        *,
        node_exe: str = "",
        dist_dir: Path | None = None,
        log_dir: Path | None = None,
        port: int = CODEXPRO_LOCAL_PORT,
        timeout_seconds: float = DEFAULT_ENGINE_START_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(node_exe or find_node(), "CodexPro")
        if not self.executable:
            raise SpawnError("未找到 node.exe。请安装 Node.js 20+ 并加入 PATH。")
        self._explicit_dist_dir = dist_dir
        self.log_dir = Path(log_dir or constants.process_log_dir())
        self.port = port
        self.timeout = timeout_seconds

    def _candidate_dist_dirs(self, root: str | None = None) -> list[Path]:
        """Candidate CodexPro build dirs across runtime layouts (dedup'd).

        Order: explicit (tests) -> env ``CODEXPRO_DIST_DIR`` -> bundled copy
        (PyInstaller ``_MEIPASS``) -> exe-adjacent copy -> source tree -> the
        project root's ``third_party/codexpro/dist`` -> ProgramData location.
        """
        candidates: list[Path] = []
        if self._explicit_dist_dir is not None:
            candidates.append(self._explicit_dist_dir)
        env_dir = os.environ.get("CODEXPRO_DIST_DIR")
        if env_dir:
            candidates.append(Path(env_dir))
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "third_party" / "codexpro" / "dist")
            candidates.append(Path(sys.executable).resolve().parent.parent / "third_party" / "codexpro" / "dist")
        candidates.append(Path(__file__).resolve().parents[2] / "third_party" / "codexpro" / "dist")
        if root:
            candidates.append(Path(root) / "third_party" / "codexpro" / "dist")
        candidates.append(
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "LocalDevMCPBridge" / "third_party" / "codexpro" / "dist"
        )
        seen: set[Path] = set()
        result: list[Path] = []
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    def _find_http_js(self, root: str | None = None) -> Path:
        """Pick a CodexPro dist whose ``http.js`` exists, preferring one whose
        sibling ``node_modules`` is present (ESM deps resolve up from dist).

        An explicitly configured dist dir is authoritative: no fallback, so a
        missing build there fails loudly (tests rely on this)."""
        if self._explicit_dist_dir is not None:
            return self._explicit_dist_dir / "http.js"
        fallback = None
        for candidate in self._candidate_dist_dirs(root):
            http_js = candidate / "http.js"
            if not http_js.is_file():
                continue
            if (candidate.parent / "node_modules").is_dir():
                return http_js
            if fallback is None:
                fallback = http_js
        if fallback is not None:
            return fallback
        return self._candidate_dist_dirs(root)[0] / "http.js"

    def start(
        self,
        root: str,
        token: str,
        *,
        permission_mode: str = "workspace",
        windows_token: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        if self.is_running:
            return
        if not token or len(token) < 24:
            self._fail("CodexPro 访问令牌长度不足（至少 24 字节）。")
            raise SpawnError("CodexPro 访问令牌长度不足。")
        self.dist_dir = self._find_http_js(root).parent
        http_js = self.dist_dir / "http.js"
        if not http_js.is_file():
            self._fail(
                f"CodexPro 构建产物缺失：{http_js}"
                "（请确认 third_party/codexpro 已执行 npm ci 与 npm run build，"
                "或设置环境变量 CODEXPRO_DIST_DIR 指向构建产物目录）"
            )
            raise SpawnError("CodexPro 构建产物缺失。")
        env = build_codex_env(
            root,
            permission_mode=permission_mode,
            token=token,
            windows_token=windows_token,
            extra=extra_env,
        )
        cmd = build_codex_cmd(self.executable, http_js, root, self.port)
        self._set_state(EngineState.STARTING)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._spawn(cmd, env, (token, windows_token or ""), self.log_dir / "codexpro.log")

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None:
                return False
            if not proc.is_running:
                self._fail("CodexPro 进程提前退出。")
                return False
            if CODEXPRO_READY_RE.search(proc.log.tail(50)):
                return True
            if port_listening(self.port):
                return True
            time.sleep(READY_POLL_INTERVAL_SECONDS)
        self._fail(f"CodexPro 启动超时（{timeout_seconds or self.timeout} 秒）。")
        return False


class WindowsBridgeManager(EngineManager):
    """Owns the uvx-run Windows-MCP bridge process (loopback only)."""

    def __init__(
        self,
        *,
        uvx_exe: str = "",
        log_dir: Path | None = None,
        port: int = WINDOWS_BRIDGE_PORT,
        timeout_seconds: float = DEFAULT_WINDOWS_START_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(uvx_exe or find_uvx(), "Windows-MCP")
        if not self.executable:
            raise SpawnError("未找到 uvx.exe。请安装 uv（https://astral.sh/uv）。")
        self.log_dir = Path(log_dir or constants.process_log_dir())
        self.port = port
        self.timeout = timeout_seconds

    def start(self, token: str, extra_env: dict[str, str] | None = None) -> None:
        if self.is_running:
            return
        if len(token or "") < 24:
            self._fail("Windows 桥接令牌长度不足（至少 24 字节）。")
            raise SpawnError("Windows 桥接令牌长度不足。")
        cmd = [
            self.executable,
            "--from",
            f"windows-mcp=={WINDOWS_MCP_PINNED_VERSION}",
            "windows-mcp",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        env = {
            "ANONYMIZED_TELEMETRY": "false",
            "WINDOWS_MCP_AUTH_KEY": token,
            "PYTHONIOENCODING": "utf-8",
            **(extra_env or {}),
        }
        self._set_state(EngineState.STARTING)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._spawn(cmd, env, (token,), self.log_dir / "windows_mcp.log")

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None:
                return False
            if not proc.is_running:
                self._fail("Windows-MCP 进程提前退出。")
                return False
            if port_listening(self.port):
                return True
            time.sleep(READY_POLL_INTERVAL_SECONDS)
        self._fail(f"Windows-MCP 启动超时（{timeout_seconds or self.timeout} 秒）。")
        return False


__all__ = [
    "CODEXPRO_LOCAL_PORT",
    "WINDOWS_BRIDGE_PORT",
    "WINDOWS_MCP_PINNED_VERSION",
    "EngineState",
    "ProcessLog",
    "SpawnError",
    "redact_line",
    "sanitize_cmd_for_log",
    "port_listening",
    "wait_port",
    "find_node",
    "find_uvx",
    "build_codex_env",
    "build_codex_cmd",
    "CodexProManager",
    "WindowsBridgeManager",
]