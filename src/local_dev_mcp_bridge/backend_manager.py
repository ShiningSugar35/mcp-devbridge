"""Desktop-side backend subprocess lifecycle management (no UI dependency).

The desktop window calls these functions from worker threads (QThreadPool);
functions are thread-safe and never touch the UI.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import constants
from .config_store import load_runtime_config, save_runtime_config
from .models import RuntimeConfig
from .secrets import SecretsStore, generate_token
from .server_main import ensure_access_token

HEALTH_TIMEOUT_SECONDS = 3


class BackendError(Exception):
    """User-facing backend lifecycle error (message is Chinese)."""


def backend_health(base_url: str, timeout: float = HEALTH_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    """GET /health; returns parsed JSON or None if unreachable."""
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = resp.read().decode("utf-8", errors="replace")
            return json.loads(payload)
    except (urllib.error.URLError, OSError, ValueError):
        return None


class BackendManager:
    """Owns the backend subprocess for the desktop app.

    Lifecycle: start() writes runtime.json and spawns `server_main`, then polls
    /health until ready (or fails); stop() terminates the process tree.
    """

    def __init__(
        self,
        python_exe: str = "",
        timeout_seconds: float = 30.0,
        config_dir: Path | None = None,
    ) -> None:
        self.python_exe = python_exe or sys.executable
        self.timeout_seconds = timeout_seconds
        self.config_dir = Path(config_dir or constants.config_dir())
        self._proc: subprocess.Popen | None = None
        self._rc_path = self.config_dir / "runtime.json"

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def runtime_config_path(self) -> Path:
        return self._rc_path

    def current_config(self) -> RuntimeConfig | None:
        return load_runtime_config(self._rc_path)

    # ------------------------------------------------------------------
    def start(self, rc: RuntimeConfig, wait_ready: bool = True) -> None:
        """Start the backend with the given runtime config."""
        if self.is_running:
            raise BackendError("后端已在运行中。")
        workspace = Path(rc.workspace)
        if not workspace.is_dir():
            raise BackendError(f"项目目录不存在：{workspace}，无法启动后端。")
        if port_in_use(rc.legacy_backend_port):
            raise BackendError(
                f"端口 {rc.legacy_backend_port} 已被占用，无法启动。请更换端口或先停止占用该端口的程序。"
            )
        self.config_dir.mkdir(parents=True, exist_ok=True)
        save_runtime_config(rc, self._rc_path)
        try:
            ensure_access_token()
        except Exception as exc:
            raise BackendError(f"访问令牌初始化失败: {exc}") from exc

        proc = subprocess.Popen(
            [
                self.python_exe,
                "-m",
                "local_dev_mcp_bridge.server_main",
                "--config",
                str(self._rc_path),
                "--port",
                str(rc.legacy_backend_port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._proc = proc

        if not wait_ready:
            return
        if not self._wait_ready(rc.legacy_backend_port):
            output = self._drain_output()
            self.stop()
            raise BackendError(f"后端启动失败（端口 {rc.legacy_backend_port} 未就绪）。\n{output}")

    def _wait_ready(self, port: int) -> bool:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            if backend_health(f"http://127.0.0.1:{port}") is not None:
                return True
            time.sleep(0.2)
        return False

    def _drain_output(self, limit: int = 4000) -> str:
        if self._proc is None or self._proc.stdout is None:
            return ""
        try:
            chunks = []
            while len(chunks) * 1024 < limit:
                line = self._proc.stdout.readline()
                if not line:
                    break
                chunks.append(line.rstrip())
                if len(chunks) > 200:
                    break
            return "\n".join(chunks)[-limit:]
        except Exception:
            return ""

    def stop(self, timeout_seconds: float = 8.0) -> None:
        """Terminate the backend (SIGTERM then kill tree on timeout)."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout_seconds)
            except Exception:
                try:
                    from .shell import kill_process_tree

                    kill_process_tree(proc.pid)
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)

    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any] | None:
        rc = self.current_config()
        if rc is None or not self.is_running:
            return None
        return backend_health(f"http://127.0.0.1:{rc.legacy_backend_port}")

    def public_url(self, public_hostname: str) -> str:
        """https://<hostname>/mcp (fixed by the tunnel)."""
        return f"https://{public_hostname.strip().rstrip('/')}/mcp"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def current_access_token() -> str | None:
    """Expose the current public-access bearer token (for UI display)."""
    try:
        return SecretsStore().get(constants.ACCESS_TOKEN_CRED_NAME)
    except Exception:
        return None


def regenerate_access_token() -> str:
    """Rotate the token; invalidates the previous value everywhere."""
    token = generate_token(256)
    SecretsStore().set(constants.ACCESS_TOKEN_CRED_NAME, token)
    return token


__all__ = [
    "BackendManager",
    "BackendError",
    "backend_health",
    "port_in_use",
    "current_access_token",
    "regenerate_access_token",
]
