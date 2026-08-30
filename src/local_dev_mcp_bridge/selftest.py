"""Bounded MCP data-plane self-test used by diagnostics and health canaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .hub_tool_contract import (
    HUB_TOOL_CONTRACT_FINGERPRINT,
    HUB_TOOL_CONTRACT_VERSION,
    HUB_TOOL_COUNT,
)


@dataclass
class SelftestResult:
    ok: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    tool_count: int = 0
    schema_fingerprint: str = ""
    hub_contract_match: bool | None = None

    def add(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": step, "ok": bool(ok), "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": self.steps,
            "error": self.error,
            "tool_count": self.tool_count,
            "schema_fingerprint": self.schema_fingerprint,
            "hub_contract_version": HUB_TOOL_CONTRACT_VERSION,
            "hub_contract_match": self.hub_contract_match,
        }


def _tool_contract_summary(tools: list[Any]) -> tuple[int, str]:
    canonical: list[dict[str, Any]] = []
    for tool in tools:
        if hasattr(tool, "model_dump"):
            raw = tool.model_dump(by_alias=True, exclude_none=True)
        elif isinstance(tool, dict):
            raw = tool
        else:
            raw = vars(tool) if hasattr(tool, "__dict__") else {}
        input_schema = raw.get("inputSchema")
        if not isinstance(input_schema, dict):
            input_schema = raw.get("input_schema")
        canonical.append(
            {
                "name": str(raw.get("name") or getattr(tool, "name", "") or ""),
                "description": str(
                    raw.get("description") or getattr(tool, "description", "") or ""
                ),
                "inputSchema": input_schema if isinstance(input_schema, dict) else {},
            }
        )
    canonical.sort(key=lambda item: item["name"])
    fingerprint = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return len(canonical), fingerprint


def _root_error(exc: BaseException) -> BaseException:
    """Return the first actionable leaf instead of exposing a bare TaskGroup wrapper."""
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            leaf = _root_error(child)
            if not isinstance(leaf, asyncio.CancelledError):
                return leaf
        if exc.exceptions:
            return _root_error(exc.exceptions[0])
    return exc


async def _run_selftest(
    url: str,
    token: str | None = None,
    *,
    timeout: float = 15.0,
    route_workspace_id: str = "",
    expect_hub_contract: bool = False,
) -> SelftestResult:
    """Exercise initialize -> tools/list -> one read-only production tool.

    The historical aliases (get_workspace_info/list_directory/get_capabilities)
    are deliberately not used: the production CodexPro schema exposes
    server_config/open_current_workspace/tree instead.  ``route_workspace_id``
    is a Gateway-only routing hint so a multi-project diagnostic tests the
    project the user actually selected rather than a lexical fallback root.
    """
    result = SelftestResult()
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request_timeout = httpx.Timeout(max(1.0, float(timeout)), connect=min(10.0, max(1.0, float(timeout))))
    try:
        async with httpx.AsyncClient(headers=headers, timeout=request_timeout) as http_client, streamable_http_client(
            url=url, http_client=http_client  # type: ignore[arg-type]
        ) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                result.add("initialize", True, "MCP initialize 成功")
                result.add("streamable_http", True, "MCP 流式连接通道已建立")

                tools = await session.list_tools()
                tool_values = list(tools.tools) if hasattr(tools, "tools") else []
                names = [str(getattr(tool, "name", "") or "") for tool in tool_values]
                result.tool_count, result.schema_fingerprint = _tool_contract_summary(tool_values)
                result.add(
                    "list_tools",
                    len(names) > 0,
                    f"发现 {len(names)} 个工具: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}",
                )
                if not names:
                    result.error = "tools/list 未返回可用工具"
                    return result
                if expect_hub_contract:
                    result.hub_contract_match = (
                        result.tool_count == HUB_TOOL_COUNT
                        and result.schema_fingerprint == HUB_TOOL_CONTRACT_FINGERPRINT
                    )
                    result.add(
                        "hub_contract",
                        bool(result.hub_contract_match),
                        (
                            f"公开工具 {result.tool_count}/{HUB_TOOL_COUNT}，"
                            f"schema {result.schema_fingerprint[:12]}…"
                        ),
                    )
                    if not result.hub_contract_match:
                        result.error = (
                            "公开工具契约不一致："
                            f"count={result.tool_count}, schema={result.schema_fingerprint[:12]}…"
                        )
                        return result

                canary = "server_config" if "server_config" in names else "open_current_workspace" if "open_current_workspace" in names else ""
                if not canary:
                    result.add("read_only_tool", False, "缺少可用于诊断的只读工具")
                    result.error = "MCP 工具表缺少只读诊断工具"
                    return result

                arguments: dict[str, Any] = {}
                if route_workspace_id:
                    arguments["devbridge_workspace_id"] = route_workspace_id
                call = await session.call_tool(canary, arguments)
                text = _extract_text(call)
                result.add(canary, bool(text), text.splitlines()[0] if text else "只读调用未返回内容")

                result.ok = all(step["ok"] for step in result.steps)
                return result
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # ExceptionGroup from AnyIO TaskGroup is a BaseExceptionGroup.
        root = _root_error(exc)
        result.error = f"{type(root).__name__}: {root}"
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


def run_selftest(
    url: str,
    token: str | None = None,
    timeout: float = 60.0,
    *,
    route_workspace_id: str = "",
    expect_hub_contract: bool = False,
) -> SelftestResult:
    """Synchronous wrapper for a bounded self-test (safe in a worker thread)."""
    return asyncio.run(
        _run_selftest(
            url,
            token,
            timeout=timeout,
            route_workspace_id=route_workspace_id,
            expect_hub_contract=expect_hub_contract,
        )
    )


__all__ = ["SelftestResult", "run_selftest"]
