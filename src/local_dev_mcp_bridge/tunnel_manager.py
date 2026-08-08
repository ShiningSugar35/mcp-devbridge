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
import time
from enum import StrEnum
from pathlib import Path

from . import constants
from .engines import READY_POLL_INTERVAL_SECONDS, EngineManager, EngineState, SpawnError

QUICK_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9\-]*\.trycloudflare\.com")
NGROK_URL_RE = re.compile(
    r"https://[a-z0-9][a-z0-9.-]*\.(?:ngrok\.(?:app|io)|ngrok-free\.app)(?:/[a-z0-9/_-]*)?"
)


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
    project_root = Path(__file__).resolve().parents[2]
    local = project_root / ".tools" / "cloudflared.exe"
    return str(local) if local.is_file() else (shutil.which("cloudflared") or "")


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

    def _apply_executable(self) -> None:
        if self.kind == ConnectionMethod.NGROK:
            if not self.ngrok:
                raise SpawnError("未找到 ngrok.exe，请安装 ngrok 并加入 PATH。")
            self.executable = self.ngrok
        else:
            if not self.cloudflared:
                raise SpawnError("未找到 cloudflared.exe（项目 .tools 目录或 PATH）。")
            self.executable = self.cloudflared

    def start(
        self,
        *,
        kind: ConnectionMethod,
        hostname: str = "",
        cloudflare_config: Path | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        if self.is_running:
            return
        self.kind = kind
        self.public_hostname = hostname.strip().rstrip("/")
        self._apply_executable()
        self._set_state(EngineState.STARTING)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        secrets = tuple(s for s in (tunnel_token,) if s)

        if kind == ConnectionMethod.LOCAL:
            # no tunnel process; the public URL is "local only"
            self._set_state(EngineState.READY)
            self.public_url = ""
            return

        if kind == ConnectionMethod.CLOUDFLARE:
            if tunnel_token:
                # 注意：`--no-autoupdate` 不可放在 `run` 子命令后（2026.7.3 解析失败会直接打印 help），故不带。
                cmd = [
                    self.executable,
                    "tunnel",
                    "run",
                    "--token",
                    tunnel_token,
                ]
            elif cloudflare_config and cloudflare_config.is_file():
                cmd = [self.executable, "tunnel", "--config", str(cloudflare_config), "run"]
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
        self._spawn(cmd, {}, secrets, self.log_dir / "tunnel.log")

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        if self.kind == ConnectionMethod.LOCAL:
            self._set_state(EngineState.READY)
            return True
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None:
                return False
            if not proc.is_running:
                self._fail("隧道进程提前退出。")
                return False
            tail = proc.log.tail(200)
            url = self._parse_public_url(tail)
            if url:
                self.public_url = url
                self._set_state(EngineState.READY)
                return True
            time.sleep(READY_POLL_INTERVAL_SECONDS)
        self._fail(f"隧道建立超时（{timeout_seconds or self.timeout} 秒）。")
        return False

    def _parse_public_url(self, tail: str) -> str:
        if self.kind == ConnectionMethod.QUICK:
            match = QUICK_TUNNEL_URL_RE.findall(tail)
            return match[-1] if match else ""
        if self.kind == ConnectionMethod.NGROK and self.public_hostname:
            match = NGROK_URL_RE.search(tail)
            if match:
                return match.group(0)
            return f"https://{self.public_hostname}" if "started tunnel" in tail else ""
        if self.kind == ConnectionMethod.CLOUDFLARE and self.public_hostname:
            # 固定域名：配置预先写好 hostname，log 出现连接确认即视为可用
            if "registered tunnel connection" in tail.lower() or "starting tunnel server" in tail.lower() or "clientconnectorregistered" in tail.lower():
                return f"https://{self.public_hostname}/mcp"
            return ""
        return ""

    def stop(self, timeout_seconds: float = 8.0) -> None:
        super().stop(timeout_seconds=timeout_seconds)
        self.public_url = ""


__all__ = [
    "ConnectionMethod",
    "TunnelManager",
    "default_cloudflared",
    "default_ngrok",
]