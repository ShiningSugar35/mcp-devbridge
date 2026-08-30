"""Tunnel subprocess management: Cloudflare Named / Quick, ngrok, local-only.

Pure-Python module (no Qt). The public MCP URL is FIXED once a named tunnel
or reserved ngrok domain is chosen; a restart never changes it. The
"Quick Tunnel 临时测试" mode is for temporary verification and its URL
changes on every run.

Secrets (cloudflare token file, ngrok config) are never logged.
"""

from __future__ import annotations

import re
import shutil
import sys
import threading
import time
from enum import StrEnum
from pathlib import Path

from . import constants
from .engines import READY_POLL_INTERVAL_SECONDS, EngineManager, EngineState, SpawnError
from .platform_support import runtime_filename

QUICK_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9\-]*\.trycloudflare\.com")
NGROK_URL_RE = re.compile(
    r"https://[a-z0-9][a-z0-9.-]*\.(?:ngrok\.(?:app|io)|ngrok-free\.app)(?:/[a-z0-9/_-]*)?"
)
_CLOUDFLARE_PROTOCOLS = frozenset({"auto", "quic", "http2"})


class ConnectionMethod(StrEnum):
    """Connection combo values persisted as the last-used setting."""

    CLOUDFLARE = "cloudflare"
    NGROK = "ngrok"
    LOCAL = "local"
    QUICK = "quick"

    def label(self) -> str:
        return {
            ConnectionMethod.CLOUDFLARE: "Cloudflare 固定地址",
            ConnectionMethod.NGROK: "ngrok 固定地址",
            ConnectionMethod.LOCAL: "仅本机",
            ConnectionMethod.QUICK: "Quick Tunnel 临时测试",
        }[self]


def default_cloudflared() -> str:
    filename = runtime_filename("cloudflared")
    if getattr(sys, "frozen", False):
        packaged = Path(sys.executable).resolve().parent / filename
        if packaged.is_file():
            return str(packaged)
    project_root = Path(__file__).resolve().parents[2]
    for local in (project_root / ".tools" / filename, project_root / ".tools" / "linux" / filename):
        if local.is_file():
            return str(local)
    return shutil.which("cloudflared") or ""


def default_ngrok() -> str:
    return shutil.which("ngrok") or shutil.which("ngrok.exe") or ""


class TunnelManager(EngineManager):
    """Owns the cloudflared or ngrok subprocess for the public endpoint."""

    def __init__(
        self,
        *,
        cloudflared_exe: str = "",
        ngrok_exe: str = "",
        log_dir: Path | None = None,
        port: int = constants.DEFAULT_CODEXPRO_PORT,
        timeout_seconds: float = 90.0,
    ) -> None:
        super().__init__("", "隧道")
        self.cloudflared = cloudflared_exe or default_cloudflared()
        self.ngrok = ngrok_exe or default_ngrok()
        self.log_dir = Path(log_dir or constants.process_log_dir())
        self.port = port
        self.timeout = timeout_seconds
        self.public_url: str = ""
        self.public_hostname: str = ""
        self.kind: ConnectionMethod = ConnectionMethod.LOCAL
        self._ready_cancel = threading.Event()
        self._last_pid: int | None = None
        self._last_exit_code: int | None = None
        self._cloudflare_protocol = "auto"
        self._recommended_protocol = ""

    @property
    def last_pid(self) -> int | None:
        return self.pid or self._last_pid

    @property
    def last_exit_code(self) -> int | None:
        proc = self._proc
        if proc is not None and not proc.is_running:
            return proc.returncode
        return self._last_exit_code

    @property
    def current_protocol(self) -> str:
        return self._cloudflare_protocol if self.kind == ConnectionMethod.CLOUDFLARE else ""

    @property
    def recommended_protocol(self) -> str:
        return self._recommended_protocol

    def _apply_executable(self) -> None:
        if self.kind == ConnectionMethod.NGROK:
            if not self.ngrok:
                raise SpawnError("未找到 ngrok，请安装后加入 PATH。")
            self.executable = self.ngrok
        else:
            if not self.cloudflared:
                raise SpawnError("未找到 cloudflared（应用私有运行时、项目 .tools 或 PATH）。")
            self.executable = self.cloudflared

    def start(
        self,
        *,
        kind: ConnectionMethod,
        hostname: str = "",
        cloudflare_config: Path | None = None,
        tunnel_token: str | None = None,
        cloudflare_protocol: str = "auto",
    ) -> None:
        if self.is_running:
            return
        self._ready_cancel.clear()
        self.kind = kind
        self.public_hostname = hostname.strip().rstrip("/")
        self._set_state(EngineState.STARTING)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        secrets = tuple(s for s in (tunnel_token,) if s)

        if kind == ConnectionMethod.LOCAL:
            # No external executable is required for loopback-only operation.
            self._set_state(EngineState.READY)
            self.public_url = ""
            return

        self._apply_executable()
        if kind == ConnectionMethod.CLOUDFLARE:
            protocol = str(cloudflare_protocol or "auto").strip().casefold()
            if protocol not in _CLOUDFLARE_PROTOCOLS:
                self._fail(f"不支持的 Cloudflare 传输协议：{protocol}")
                raise SpawnError("Cloudflare 传输协议配置无效。")
            self._cloudflare_protocol = protocol
            self._recommended_protocol = ""
            protocol_args = [] if protocol == "auto" else ["--protocol", protocol]
            if tunnel_token:
                # 默认 auto 保持历史命令；只有显式 fallback/override 才追加 protocol 参数。
                cmd = [
                    self.executable,
                    "tunnel",
                    *protocol_args,
                    "run",
                    "--token",
                    tunnel_token,
                ]
            elif cloudflare_config and cloudflare_config.is_file():
                cmd = [
                    self.executable,
                    "tunnel",
                    *protocol_args,
                    "--config",
                    str(cloudflare_config),
                    "run",
                ]
            else:
                self._fail("未配置 Cloudflare 隧道（需要隧道令牌或配置文件）。")
                raise SpawnError("未配置 Cloudflare 隧道。")
        elif kind == ConnectionMethod.NGROK:
            if not self.public_hostname:
                self._fail("ngrok 固定地址需要已保留的域名。")
                raise SpawnError("缺少 ngrok 域名。")
            cmd = [self.executable, "http", str(self.port), "--url", self.public_hostname, "--log=stdout"]
        elif kind == ConnectionMethod.QUICK:
            cmd = [self.executable, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"]
        else:
            self._fail(f"未知连接方式：{kind}")
            raise SpawnError(f"未知连接方式：{kind}")
        proc = self._spawn(cmd, {}, secrets, self.log_dir / "tunnel.log")
        self._last_pid = proc.pid
        self._last_exit_code = None

    def cancel_wait_ready(self) -> None:
        """Interrupt an in-progress readiness wait without marking failure."""

        self._ready_cancel.set()

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        if self.kind == ConnectionMethod.LOCAL:
            self._set_state(EngineState.READY)
            return True
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        while time.monotonic() < deadline:
            if self._ready_cancel.is_set():
                return False
            proc = self._proc
            if proc is None:
                return False
            if not proc.is_running:
                self._last_pid = proc.pid
                self._last_exit_code = proc.returncode
                self._fail("隧道进程提前退出。")
                return False
            tail = proc.log.tail(200)
            url = self._parse_public_url(tail)
            if url:
                self.public_url = url
                self._set_state(EngineState.READY)
                return True
            if (
                self.kind == ConnectionMethod.CLOUDFLARE
                and self._cloudflare_protocol == "auto"
            ):
                protocol_hint = self._parse_cloudflare_protocol_hint(tail)
                if protocol_hint:
                    self._recommended_protocol = protocol_hint
                    self._last_pid = proc.pid
                    self._fail(
                        "Cloudflare QUIC 暂不可用，切换 HTTP/2 重试。 "
                        f"[cloudflare_protocol_fallback:{protocol_hint}]"
                    )
                    self._last_exit_code = proc.returncode
                    return False
            if self._ready_cancel.wait(READY_POLL_INTERVAL_SECONDS):
                return False
        if self._ready_cancel.is_set():
            return False
        proc = self._proc
        if proc is not None:
            self._last_pid = proc.pid
        self._fail(f"隧道建立超时（{timeout_seconds or self.timeout} 秒）。")
        if proc is not None:
            self._last_exit_code = proc.returncode
        return False

    @staticmethod
    def _parse_cloudflare_protocol_hint(tail: str) -> str:
        lowered = tail.casefold()
        if (
            "suggested_protocol=http2" in lowered
            or "proceed using 'http2'" in lowered
            or 'proceed using "http2"' in lowered
        ):
            return "http2"
        return ""

    def _parse_public_url(self, tail: str) -> str:
        if self.kind == ConnectionMethod.QUICK:
            match = QUICK_TUNNEL_URL_RE.findall(tail)
            return f"{match[-1].rstrip('/')}/mcp" if match else ""
        if self.kind == ConnectionMethod.NGROK and self.public_hostname:
            match = NGROK_URL_RE.search(tail)
            if match:
                return f"{match.group(0).rstrip('/')}/mcp"
            return f"https://{self.public_hostname.rstrip('/')}/mcp" if "started tunnel" in tail else ""
        if self.kind == ConnectionMethod.CLOUDFLARE and self.public_hostname:
            # 固定域名：配置预先写好 hostname，log 出现连接确认即视为可用
            if "registered tunnel connection" in tail.lower() or "starting tunnel server" in tail.lower() or "clientconnectorregistered" in tail.lower():
                return f"https://{self.public_hostname}/mcp"
            return ""
        return ""

    def stop(self, timeout_seconds: float = 8.0) -> None:
        self._ready_cancel.set()
        proc = self._proc
        if proc is not None:
            self._last_pid = proc.pid
        super().stop(timeout_seconds=timeout_seconds)
        if proc is not None:
            self._last_exit_code = proc.returncode
        self.public_url = ""


__all__ = [
    "ConnectionMethod",
    "TunnelManager",
    "default_cloudflared",
    "default_ngrok",
]