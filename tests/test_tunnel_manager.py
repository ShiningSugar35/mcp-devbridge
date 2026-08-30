"""Tests for tunnel_manager.py: URL parsing, command building, lifecycle."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from local_dev_mcp_bridge.engines import SpawnError
from local_dev_mcp_bridge.platform_support import runtime_filename
from local_dev_mcp_bridge.tunnel_manager import (
    ConnectionMethod,
    TunnelManager,
    default_cloudflared,
    default_ngrok,
)


class TestConnectionMethod:
    def test_labels(self) -> None:
        assert ConnectionMethod.CLOUDFLARE.label() == "Cloudflare 固定地址"
        assert ConnectionMethod.NGROK.label() == "ngrok 固定地址"
        assert ConnectionMethod.LOCAL.label() == "仅本机"
        assert ConnectionMethod.QUICK.label() == "Quick Tunnel 临时测试"


class TestUrlParsing:
    def test_quick_parse(self) -> None:
        mgr = TunnelManager(cloudflared_exe="cloudflared", port=8787)
        mgr.kind = ConnectionMethod.QUICK
        tail = "[INF] Registered tunnel connection https://abc-123.trycloudflare.com"
        assert mgr._parse_public_url(tail) == "https://abc-123.trycloudflare.com/mcp"

    def test_quick_parse_no_url_yet(self) -> None:
        mgr = TunnelManager(cloudflared_exe="cloudflared", port=8787)
        mgr.kind = ConnectionMethod.QUICK
        assert mgr._parse_public_url("starting...") == ""

    def test_ngrok_parse_uses_fixed_hostname(self) -> None:
        mgr = TunnelManager(ngrok_exe="ngrok", port=8787)
        mgr.kind = ConnectionMethod.NGROK
        mgr.public_hostname = "my-dev.ngrok.app"
        tail = 't=2026-08-08T00:00:00 msg="started tunnel" url=https://my-dev.ngrok.app'
        assert mgr._parse_public_url(tail) == "https://my-dev.ngrok.app/mcp"

    def test_ngrok_waits_for_started(self) -> None:
        mgr = TunnelManager(ngrok_exe="ngrok", port=8787)
        mgr.kind = ConnectionMethod.NGROK
        mgr.public_hostname = "my-dev.ngrok.app"
        assert mgr._parse_public_url("connecting...") == ""

    def test_cloudflare_fixed_hostname_on_connect(self) -> None:
        mgr = TunnelManager(cloudflared_exe="cloudflared", port=8787)
        mgr.kind = ConnectionMethod.CLOUDFLARE
        mgr.public_hostname = "bridge.example.com"
        tail = "[INF] Registered tunnel connection connId=abc"
        assert mgr._parse_public_url(tail) == "https://bridge.example.com/mcp"

    def test_cloudflare_requires_connection_confirmation(self) -> None:
        mgr = TunnelManager(cloudflared_exe="cloudflared", port=8787)
        mgr.kind = ConnectionMethod.CLOUDFLARE
        mgr.public_hostname = "bridge.example.com"
        assert mgr._parse_public_url("starting...") == ""


class TestLifecycle:
    def test_local_ready_without_process(self, tmp_path: Path) -> None:
        mgr = TunnelManager(port=8787, cloudflared_exe="cloudflared", log_dir=tmp_path)
        mgr.start(kind=ConnectionMethod.LOCAL, hostname="")
        assert mgr.state.value == "已连接"
        assert mgr.public_url == ""

    def test_local_does_not_require_cloudflared(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_dev_mcp_bridge.tunnel_manager.default_cloudflared", lambda: "")
        mgr = TunnelManager(port=8787, cloudflared_exe="", log_dir=tmp_path)
        mgr.start(kind=ConnectionMethod.LOCAL, hostname="")
        assert mgr.state.value == "已连接"
        assert mgr.public_url == ""

    def test_missing_cloudflared_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_dev_mcp_bridge.tunnel_manager.default_cloudflared", lambda: "")
        mgr = TunnelManager(port=8787, cloudflared_exe="", log_dir=tmp_path)
        with pytest.raises(Exception, match="cloudflared"):
            mgr.start(kind=ConnectionMethod.QUICK)

    def test_missing_ngrok_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("local_dev_mcp_bridge.tunnel_manager.default_ngrok", lambda: "")
        mgr = TunnelManager(port=8787, cloudflared_exe="cloudflared", ngrok_exe="", log_dir=tmp_path)
        with pytest.raises(Exception, match="ngrok"):
            mgr.start(kind=ConnectionMethod.NGROK, hostname="x.ngrok.app")

    def test_missing_cloudflare_credentials_raises(self, tmp_path: Path) -> None:
        mgr = TunnelManager(port=8787, cloudflared_exe="cloudflared", log_dir=tmp_path)
        with pytest.raises(SpawnError):
            mgr.start(kind=ConnectionMethod.CLOUDFLARE, hostname="x.example.com")
        assert mgr.error is not None

    def test_cloudflare_token_runs_named_tunnel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_spawn(self, cmd, env, secrets, log_file) -> object:  # noqa: ARG002 - self unused
            captured.append(list(cmd))
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(TunnelManager, "_spawn", fake_spawn)
        mgr = TunnelManager(port=8787, cloudflared_exe="cloudflared", log_dir=tmp_path)
        mgr.start(
            kind=ConnectionMethod.CLOUDFLARE,
            hostname="mcp.shiningsugar.shop",
            tunnel_token="tok-secret-123",
        )
        assert captured == [["cloudflared", "tunnel", "run", "--token", "tok-secret-123"]]
        assert mgr.public_hostname == "mcp.shiningsugar.shop"


    def test_wait_ready_can_be_cancelled_without_waiting_for_full_timeout(
        self, tmp_path: Path
    ) -> None:
        mgr = TunnelManager(
            port=8787,
            cloudflared_exe="cloudflared",
            log_dir=tmp_path,
            timeout_seconds=90.0,
        )
        mgr.kind = ConnectionMethod.CLOUDFLARE
        mgr.public_hostname = "mcp.example.com"
        mgr._proc = cast(
            Any,
            SimpleNamespace(
                is_running=True,
                log=SimpleNamespace(tail=lambda _limit: "starting tunnel"),
            ),
        )
        results: list[bool] = []
        waiter = threading.Thread(target=lambda: results.append(mgr.wait_ready()), daemon=True)
        waiter.start()
        time.sleep(0.05)

        started = time.monotonic()
        mgr.cancel_wait_ready()
        waiter.join(timeout=0.5)
        elapsed = time.monotonic() - started

        assert elapsed < 0.5
        assert not waiter.is_alive()
        assert results == [False]
        assert mgr.error is None


    def test_cloudflare_auto_precheck_requests_http2_fallback_without_waiting_full_timeout(
        self, tmp_path: Path
    ) -> None:
        stopped: list[float] = []
        proc = SimpleNamespace(
            pid=9001,
            is_running=True,
            returncode=None,
            log=SimpleNamespace(
                tail=lambda _limit: (
                    "precheck complete hard_fail=false "
                    "suggested_protocol=http2\n"
                )
            ),
            stop=lambda timeout_seconds=5.0: stopped.append(timeout_seconds),
        )
        mgr = TunnelManager(
            port=8787,
            cloudflared_exe="cloudflared",
            log_dir=tmp_path,
            timeout_seconds=90.0,
        )
        mgr.kind = ConnectionMethod.CLOUDFLARE
        mgr.public_hostname = "mcp.example.com"
        mgr._proc = cast(Any, proc)
        mgr._cloudflare_protocol = "auto"  # type: ignore[attr-defined]

        started = time.monotonic()
        assert mgr.wait_ready(timeout_seconds=0.2) is False
        elapsed = time.monotonic() - started

        assert elapsed < 0.15
        assert mgr.recommended_protocol == "http2"
        assert "cloudflare_protocol_fallback:http2" in str(mgr.error)
        assert stopped

    def test_cloudflare_http2_override_is_explicit_in_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_spawn(self, cmd, env, secrets, log_file):  # noqa: ANN001, ANN202, ARG002
            captured.append(list(cmd))
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(TunnelManager, "_spawn", fake_spawn)
        mgr = TunnelManager(port=8787, cloudflared_exe="cloudflared", log_dir=tmp_path)
        mgr.start(
            kind=ConnectionMethod.CLOUDFLARE,
            hostname="mcp.example.com",
            tunnel_token="dummy-token",
            cloudflare_protocol="http2",
        )

        assert mgr.current_protocol == "http2"
        assert captured == [
            [
                "cloudflared",
                "tunnel",
                "--protocol",
                "http2",
                "run",
                "--token",
                "dummy-token",
            ]
        ]


class TestDefaults:
    def test_cloudflared_default_looks_at_local_tools_dir(self) -> None:
        assert isinstance(default_cloudflared(), str)

    def test_cloudflared_default_uses_packaged_exe_when_frozen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packaged = tmp_path / runtime_filename("cloudflared")
        packaged.write_bytes(b"stub")
        monkeypatch.setattr("local_dev_mcp_bridge.tunnel_manager.sys.frozen", True, raising=False)
        monkeypatch.setattr(
            "local_dev_mcp_bridge.tunnel_manager.sys.executable",
            str(tmp_path / runtime_filename("MCPDevBridge")),
        )
        assert default_cloudflared() == str(packaged)

    def test_ngrok_default_is_path_probe(self) -> None:
        assert isinstance(default_ngrok(), str)