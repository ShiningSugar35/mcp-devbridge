"""Read-only diagnostics for the dependencies and the Live relay."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

import httpx

from .engines import find_node
from .live_agent import _service_url
from .live_profile import _profile_path


def diagnostic_log_path() -> Path:
    return _profile_path().with_name("MCP-Live.log")


def load_events() -> list[str]:
    try:
        path = diagnostic_log_path()
        if path.stat().st_size <= 512_000:
            return path.read_text(encoding="utf-8").splitlines()[-200:]
    except (OSError, UnicodeError):
        pass
    return []


def save_events(events: Iterable[str]) -> None:
    with suppress(OSError):
        path = diagnostic_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(events) + "\n", encoding="utf-8")


def check_connection(endpoint: str) -> tuple[str, ...]:
    results = ["Node 运行时已找到。" if find_node() else "未找到 Node 运行时。"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        script = Path(bundle) / "third_party" / "codexpro" / "dist" / "http.js"
        results.append("已找到随包的 CodexPro 引擎。" if script.is_file() else "缺少随包的 CodexPro 引擎。")
    try:
        origin = _service_url(endpoint)
        response = httpx.get(origin + "/health", timeout=8, trust_env=False)
        health = response.json() if response.status_code == 200 else None
        if isinstance(health, dict) and health.get("status") == "ok":
            results.append("公网中转 /health 正常（HTTP 200）。")
        else:
            results.append(f"公网中转健康检查异常（HTTP {response.status_code}）。")
    except ValueError:
        results.append("服务地址或健康检查响应格式无效。")
    except httpx.HTTPError as error:
        results.append(f"公网中转访问失败（{type(error).__name__}）。")
    return tuple(results)
