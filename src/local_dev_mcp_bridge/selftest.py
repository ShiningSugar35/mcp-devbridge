"""Local MCP client self-test used by the desktop "test connection" button."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


@dataclass
class SelftestResult:
    ok: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def add(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": step, "ok": bool(ok), "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": self.steps, "error": self.error}


async def _run_selftest(url: str, token: str | None = None) -> SelftestResult:
    result = SelftestResult()
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as http_client, streamable_http_client(
            url=url, http_client=http_client  # type: ignore[arg-type]
        ) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                    await session.initialize()
                    result.add("initialize", True, "MCP initialize 成功")

                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools] if hasattr(tools, "tools") else []
                    result.add(
                        "list_tools",
                        len(names) > 0,
                        f"发现 {len(names)} 个工具: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}",
                    )

                    info = await session.call_tool("get_workspace_info", {})
                    info_text = _extract_text(info)
                    result.add("get_workspace_info", bool(info_text), info_text.splitlines()[0] if info_text else "")

                    ls = await session.call_tool("list_directory", {})
                    ls_text = _extract_text(ls)
                    result.add("list_directory", bool(ls_text), (ls_text.splitlines()[0] if ls_text else ""))

                    caps = await session.call_tool("get_capabilities", {})
                    caps_text = _extract_text(caps)
                    result.add("get_capabilities", bool(caps_text), "")

                    result.ok = all(s["ok"] for s in result.steps)
                    return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.ok = False
        return result


def _extract_text(call_result: Any) -> str:
    content = getattr(call_result, "content", None) or []
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def run_selftest(url: str, token: str | None = None, timeout: float = 60.0) -> SelftestResult:
    """Synchronous wrapper for the self-test (safe to call from a worker thread)."""
    return asyncio.run(_run_selftest(url, token))


__all__ = ["SelftestResult", "run_selftest"]