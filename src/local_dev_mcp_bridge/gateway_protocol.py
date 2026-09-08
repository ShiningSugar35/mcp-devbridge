"""Bounded MCP ingress validation; no routing, credentials, or user-result caching."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from starlette.requests import ClientDisconnect, Request


class McpRequestError(ValueError):
    def __init__(
        self, message: str, *, code: int = -32600, status: int = 400, rpc_id: Any = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.rpc_id = rpc_id


async def read_mcp_body(request: Request, *, max_bytes: int, timeout_seconds: float) -> bytes:
    """Enforce both declared and actual size; a slow sender cannot hold the Hub forever."""
    lengths = request.headers.getlist("content-length")
    if lengths:
        try:
            declared = int(lengths[0])
        except ValueError:
            raise McpRequestError("Invalid Content-Length.") from None
        if len(lengths) != 1 or declared < 0:
            raise McpRequestError("Invalid Content-Length.")
        if declared > max_bytes:
            raise McpRequestError("MCP request exceeds the byte limit.", status=413)
    body = bytearray()
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_bytes:
                    raise McpRequestError("MCP request exceeds the byte limit.", status=413)
                body.extend(chunk)
    except TimeoutError:
        raise McpRequestError("MCP request body deadline exceeded.", status=408) from None
    except ClientDisconnect:
        raise McpRequestError("MCP request body disconnected.") from None
    return bytes(body)


def parse_mcp_envelope(body: bytes) -> dict[str, Any]:
    """Validate the transport envelope, leaving method-specific schemas to the SDK."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise McpRequestError("Invalid JSON.", code=-32700) from None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise McpRequestError("Expected one JSON-RPC 2.0 object.")
    rpc_id = value.get("id")
    if (rpc_id is not None and not isinstance(rpc_id, (str, int, float))) or isinstance(
        rpc_id, bool
    ):
        raise McpRequestError("Invalid JSON-RPC id.")
    if isinstance(rpc_id, float) and not math.isfinite(rpc_id):
        raise McpRequestError("Invalid JSON-RPC id.")
    if "method" not in value:
        # Legacy client responses are valid transport messages, not tool calls.
        if "id" in value and (("result" in value) != ("error" in value)):
            return value
        raise McpRequestError("Missing JSON-RPC method.", rpc_id=rpc_id)
    if not isinstance(value["method"], str) or not value["method"]:
        raise McpRequestError("Invalid JSON-RPC method.", rpc_id=rpc_id)
    if "params" in value and not isinstance(value["params"], dict):
        raise McpRequestError("MCP params must be an object.", code=-32602, rpc_id=rpc_id)
    if value["method"] == "tools/call":
        params = value.get("params", {})
        if not isinstance(params.get("name"), str) or not params["name"]:
            raise McpRequestError(
                "Tool name must be a nonempty string.", code=-32602, rpc_id=rpc_id
            )
        if "arguments" in params and not isinstance(params["arguments"], dict):
            raise McpRequestError("Tool arguments must be an object.", code=-32602, rpc_id=rpc_id)
    return value
