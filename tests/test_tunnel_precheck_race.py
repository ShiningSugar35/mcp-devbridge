from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod, TunnelManager


def _manager(tmp_path: Path, tail) -> tuple[TunnelManager, list[float]]:  # noqa: ANN001
    stopped: list[float] = []
    proc = SimpleNamespace(
        pid=9002,
        is_running=True,
        returncode=None,
        log=SimpleNamespace(tail=tail),
        stop=lambda timeout_seconds=5.0: stopped.append(timeout_seconds),
    )
    manager = TunnelManager(
        port=8787,
        cloudflared_exe="cloudflared",
        log_dir=tmp_path,
        timeout_seconds=1.0,
    )
    manager.kind = ConnectionMethod.CLOUDFLARE
    manager.public_hostname = "mcp.example.test"
    manager._proc = cast(Any, proc)
    return manager, stopped


def test_auto_tunnel_waits_for_late_http2_precheck(tmp_path: Path) -> None:
    tail_calls = 0

    def dynamic_tail(_limit: int) -> str:
        nonlocal tail_calls
        tail_calls += 1
        base = "Registered tunnel connection connIndex=0 protocol=quic\n"
        if tail_calls == 1:
            return base
        return base + "precheck complete hard_fail=false suggested_protocol=http2\n"

    manager, stopped = _manager(tmp_path, dynamic_tail)
    manager._cloudflare_protocol = "auto"  # type: ignore[attr-defined]

    assert manager.wait_ready(timeout_seconds=0.6) is False
    assert tail_calls >= 2
    assert manager.recommended_protocol == "http2"
    assert stopped


def test_auto_tunnel_accepts_registered_connection_after_precheck_settles(tmp_path: Path) -> None:
    text = (
        "Registered tunnel connection connIndex=0 protocol=quic\n"
        "precheck complete hard_fail=false suggested_protocol=quic\n"
    )
    manager, stopped = _manager(tmp_path, lambda _limit: text)
    manager._cloudflare_protocol = "auto"  # type: ignore[attr-defined]

    assert manager.wait_ready(timeout_seconds=0.6) is True
    assert manager.public_url == "https://mcp.example.test/mcp"
    assert manager.recommended_protocol == ""
    assert stopped == []


def test_auto_tunnel_without_precheck_never_exceeds_existing_timeout_budget(tmp_path: Path) -> None:
    text = "Registered tunnel connection connIndex=0 protocol=quic\n"
    manager, stopped = _manager(tmp_path, lambda _limit: text)
    manager._cloudflare_protocol = "auto"  # type: ignore[attr-defined]

    started = time.monotonic()
    assert manager.wait_ready(timeout_seconds=0.2) is True
    elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 0.45
    assert manager.public_url == "https://mcp.example.test/mcp"
    assert stopped == []


def test_explicit_http2_does_not_wait_for_auto_precheck(tmp_path: Path) -> None:
    text = "Registered tunnel connection connIndex=0 protocol=http2\n"
    manager, stopped = _manager(tmp_path, lambda _limit: text)
    manager._cloudflare_protocol = "http2"  # type: ignore[attr-defined]

    started = time.monotonic()
    assert manager.wait_ready(timeout_seconds=0.6) is True
    assert time.monotonic() - started < 0.1
    assert stopped == []
