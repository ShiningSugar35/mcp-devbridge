"""An isolated MCP relay for one explicitly authorised temporary device."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .gateway import _STABLE_HUB_TOOLS
from .hub_tool_contract import HUB_TOOL_CONTRACT_VERSION
from .live_device_key import DeviceKey
from .live_download import install_download
from .live_oauth import LiveOAuth
from .live_protocol import MAX_FOLDERS, POLL_SECONDS, TEXT_LIMIT, folder_alias, folder_path
from .server_factory import build_transport_security
from .tools import read_tool

DEFAULT_PORT = 8796
PAIR_SECONDS = 600
DEVICE_SECONDS = 45
OPERATION_SECONDS = 12
MAX_BODY_BYTES = 65_536
MCP_PATH = "/mcp"
MAX_PENDING_READS = 8
MAX_BRIDGE_BODY = 8 * 1024 * 1024
BRIDGE_SECONDS = 135


class LiveError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _DeviceOccupied(LiveError):
    def __init__(self, handle: str) -> None:
        super().__init__(409, "已有电脑在线，请先断开原电脑。")
        self.handle = handle


@dataclass
class _Operation:
    id: str
    payload: dict[str, Any]
    result: asyncio.Future[tuple[str | dict[str, Any] | None, str | None]]
    delivered: bool = False


@dataclass
class _Device:
    handle: str
    token: str = field(repr=False)
    name: str
    expires: float
    folders: tuple[str, ...] = ()
    bridge_id: str = ""
    require_device_id: bool = False
    operation: _Operation | None = None
    polling: bool = False
    pending_reads: int = 0
    read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _matches(value: str, expected: str) -> bool:
    return hmac.compare_digest(value.encode(), expected.encode())


class DeviceSlot:
    """Called on one asyncio event loop; never queue work for a replacement device."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pair: tuple[str, float] | None = None
        self._device: _Device | None = None
        self._changed = asyncio.Event()

    def issue_code(self) -> str:
        code = secrets.token_urlsafe(32)
        self._pair = (code, self._clock() + PAIR_SECONDS)
        return code

    def accepts_pair(self, code: str) -> bool:
        pair = self._pair
        return bool(pair and pair[1] > self._clock() and _matches(code, pair[0]))

    def connect(
        self,
        code: str,
        name: str,
        folders: tuple[str, ...] = (),
        *,
        verified_key: bool = False,
        bridge_id: str = "",
        takeover_handle: str = "",
    ) -> str:
        if not verified_key and not self.accepts_pair(code):
            raise LiveError(401, "设备密钥错误，或配对码无效。")
        current = self._current()
        if current is not None and takeover_handle != current.handle:
            raise _DeviceOccupied(current.handle)
        now = self._clock()
        device = _Device(
            handle=secrets.token_urlsafe(24),
            token=secrets.token_urlsafe(32),
            name=name,
            expires=now + DEVICE_SECONDS,
            folders=folders,
            bridge_id=bridge_id,
            require_device_id=bool(takeover_handle),
        )
        if current is not None:
            self._drop()
        if not verified_key:
            self._pair = None
        self._device = device
        return device.token

    def status(self) -> dict[str, bool | str]:
        device = self._current()
        if device is None:
            return {"online": False}
        status: dict[str, bool | str] = {
            "online": True, "name": device.name, "device_handle": device.handle
        }
        if device.folders:
            status["folders"] = "\n".join(device.folders)
        if device.bridge_id:
            status["bridge"] = "DevBridge 0.8.9"
            status["devbridge_device_id"] = device.bridge_id
        return status

    def heartbeat(self, token: str) -> None:
        self._authenticate(token).expires = self._clock() + DEVICE_SECONDS

    def disconnect(self, token: str) -> None:
        self._authenticate(token)
        self._drop()

    def revoke(self) -> None:
        self._pair = None
        self._drop()

    def _drop(self) -> None:
        device, self._device = self._device, None
        if device is not None and (operation := device.operation) and not operation.result.done():
            operation.result.set_result((None, "目标电脑已离线。"))
        self._changed.set()

    def _current(self) -> _Device | None:
        if self._device is not None and self._device.expires <= self._clock():
            self._drop()
        return self._device

    def _authenticate(self, token: str) -> _Device:
        device = self._current()
        if device is None or not _matches(token, device.token):
            raise LiveError(401, "设备凭据无效或接入已经结束。")
        return device

    async def next_operation(self, token: str) -> dict[str, Any] | None:
        device = self._authenticate(token)
        if device.polling:
            raise LiveError(409, "该设备已有在线请求。")
        device.expires = self._clock() + DEVICE_SECONDS
        operation = device.operation
        if operation is None or operation.delivered:
            self._changed.clear()
            device.polling = True
            try:
                await asyncio.wait_for(self._changed.wait(), POLL_SECONDS)
            except TimeoutError:
                return None
            finally:
                device.polling = False
            self._authenticate(token)
            operation = device.operation
        if operation is None or operation.delivered or operation.result.done():
            return None
        operation.delivered = True
        return {"id": operation.id, **operation.payload}

    def finish(
        self, token: str, request_id: str, value: str | dict[str, Any] | None, error: str | None
    ) -> None:
        device = self._authenticate(token)
        operation = device.operation
        if (
            operation is None
            or operation.id != request_id
            or not operation.delivered
            or operation.result.done()
        ):
            raise LiveError(409, "请求已结束或不属于本次接入。")
        if error is None and (("tool" in operation.payload) != isinstance(value, dict)):
            raise LiveError(400, "结果与请求类型不一致。")
        device.expires = self._clock() + DEVICE_SECONDS
        operation.result.set_result((value, error))

    async def read(self, handle: str, path: str) -> str:
        device = self._current()
        if device is None:
            raise LiveError(409, "当前没有电脑在线。")
        if handle != device.handle:
            raise LiveError(409, "目标电脑已变更。请重新确认要操作的电脑。")
        if len(device.folders) > 1:
            try:
                alias, _ = folder_path(path)
            except ValueError as error:
                raise LiveError(400, str(error)) from None
            if alias not in {name.casefold() for name in device.folders}:
                raise LiveError(400, "该文件夹未授权。请从 live_status 的 folders 选择。")
        text = await self._request(device, {"path": path}, OPERATION_SECONDS)
        if not isinstance(text, str):
            raise LiveError(502, "无法解析文本读取结果。")
        return text

    async def call_bridge(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        device = self._current()
        if device is None or not device.bridge_id:
            raise LiveError(409, "请先用新版 MCP Live 便携端启动带 0.8.9 工具链的共享。")
        target = arguments.get("devbridge_device_id")
        if target and target != device.bridge_id:
            raise LiveError(409, "目标电脑已变更。请重新确认要操作的电脑。")
        if device.require_device_id and not target and name not in {
            "devbridge_list_devices", "devbridge_get_current_device",
        }:
            raise LiveError(409, "当前电脑通过接管上线。请先用 live_status 确认，再传入 devbridge_device_id。")
        result = await self._request(
            device, {"tool": {"name": name, "arguments": arguments}}, BRIDGE_SECONDS
        )
        if not isinstance(result, dict):
            raise LiveError(502, "无法解析工具结果。")
        return CallToolResult.model_validate(result)

    async def _request(
        self, device: _Device, payload: dict[str, Any], timeout: int
    ) -> str | dict[str, Any]:
        if device.pending_reads >= MAX_PENDING_READS:
            raise LiveError(409, "读取请求过多，请稍后再试。")
        device.pending_reads += 1
        try:
            async with asyncio.timeout(2 * timeout), device.read_lock:
                if self._current() is not device:
                    raise LiveError(409, "目标电脑已离线或变更。")
                operation = _Operation(
                    id=secrets.token_urlsafe(18),
                    payload=payload,
                    result=asyncio.get_running_loop().create_future(),
                )
                device.operation = operation
                self._changed.set()
                try:
                    value, error = await asyncio.wait_for(operation.result, timeout)
                    if self._current() is not device:
                        raise LiveError(409, "目标电脑已离线或变更。")
                    if error is not None or value is None:
                        raise LiveError(400, error or "未收到设备结果。")
                    return value
                finally:
                    device.operation = None
        except TimeoutError:
            raise LiveError(504, "目标电脑未及时响应。") from None
        finally:
            device.pending_reads -= 1


class _LiveMcp(MCPServer):
    def __init__(self, slot: DeviceSlot, **kwargs: Any) -> None:
        self._slot = slot
        self._hub_tools = {tool["name"]: Tool.model_validate(tool) for tool in _STABLE_HUB_TOOLS}
        super().__init__(**kwargs)

    async def list_tools(self) -> list[Tool]:
        return [*await super().list_tools(), *self._hub_tools.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name not in self._hub_tools:
            return await super().call_tool(name, arguments, context)
        try:
            return await self._slot.call_bridge(name, arguments)
        except LiveError as error:
            return CallToolResult(content=[TextContent(type="text", text=str(error))], is_error=True)


class _Join(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    folders: Annotated[list[str], Field(max_length=MAX_FOLDERS)] = Field(default_factory=list)
    bridge_id: Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{32})?$")] = ""
    bridge_contract: int | None = None
    takeover_handle: Annotated[str, StringConstraints(pattern=r"^(?:[A-Za-z0-9_-]{32})?$")] = ""


class _Result(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=80)]
    text: Annotated[str | None, Field(max_length=TEXT_LIMIT)] = None
    tool_result: dict[str, Any] | None = None
    error: Annotated[str | None, Field(max_length=250)] = None


async def _body[T: BaseModel](
    request: Request, model: type[T], limit: int = MAX_BODY_BYTES
) -> T:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise LiveError(413, "请求体过大。")
        body.extend(chunk)
    try:
        return model.model_validate_json(body)
    except (ValidationError, ValueError):
        raise LiveError(400, "请求格式无效。") from None


def _bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) == 1:
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    raise LiveError(401, "需要访问凭据。")


def _require(request: Request, expected: str) -> None:
    if not _matches(_bearer(request), expected):
        raise LiveError(401, "访问凭据无效。")


def _json(payload: dict, status: int = 200) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(payload, status_code=status, headers=headers)


class _AuthorizeMcp:
    def __init__(self, app: ASGIApp, access_token: str, oauth: LiveOAuth | None) -> None:
        self._app = app
        self._access_token = access_token
        self._oauth = oauth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == MCP_PATH:
            token = ""
            with suppress(LiveError):
                token = _bearer(Request(scope))
            if not _matches(token, self._access_token) and not (
                token and self._oauth and await self._oauth.verify_access(token)
            ):
                response = _json({"error": "需要有效的访问凭据。"}, 401)
                if self._oauth:
                    response.headers["WWW-Authenticate"] = self._oauth.challenge
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def create_app(
    owner_token: str,
    access_token: str,
    public_hostname: str = "",
    *,
    oauth: LiveOAuth | None = None,
    device_key: DeviceKey | None = None,
    download_file: Path | None = None,
) -> Starlette:
    for token in (owner_token, access_token):
        if len(token) < 32 or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("MCP Live 需要两个独立的随机访问凭据（每个至少 32 字符）。")
    if _matches(owner_token, access_token):
        raise ValueError("所有者凭据和 MCP 访问凭据必须不同。")

    if oauth and oauth.approval_token in {owner_token, access_token}:
        raise ValueError("OAuth 批准凭据必须与设备管理和 MCP 访问凭据分开。")

    slot = DeviceSlot()
    mcp = _LiveMcp(
        slot,
        name="mcp-live",
        version=__version__,
        instructions=(
            "调用 live_status 查看当前电脑和 folders。读取时传入该电脑的 device_handle。"
            "存在多个授权文件夹时，path 使用 文件夹名称/相对路径。"
            "调用开发工具时传入 live_status 返回的 devbridge_device_id。"
            "如果已有设备身份失效，先让用户确认新电脑，再继续操作。"
        ),
    )

    @mcp.tool(annotations=read_tool())
    async def live_status() -> dict[str, bool | str]:
        """查看当前临时接入的电脑名称和设备身份。"""
        return slot.status()

    @mcp.tool(annotations=read_tool())
    async def live_read(
        device_handle: Annotated[str, Field(min_length=1, max_length=80)],
        path: Annotated[str, Field(min_length=1, max_length=1024)],
    ) -> dict[str, str]:
        """读取授权目录内的文本。多目录时 path 为 文件夹名称/相对路径。"""
        try:
            return {"text": await slot.read(device_handle, path)}
        except LiveError as error:
            raise ToolError(str(error)) from None

    async def health(_: Request) -> JSONResponse:
        return _json({"status": "ok"})

    async def pair(request: Request) -> JSONResponse:
        _require(request, owner_token)
        return _json({"pair_code": slot.issue_code(), "expires_in": PAIR_SECONDS})

    async def revoke(request: Request) -> JSONResponse:
        _require(request, owner_token)
        slot.revoke()
        return _json({"ok": True})

    async def connect(request: Request) -> JSONResponse:
        code = _bearer(request)
        join = await _body(request, _Join)
        try:
            folders = tuple(folder_alias(name) for name in join.folders)
        except ValueError as error:
            raise LiveError(400, str(error)) from None
        if len({name.casefold() for name in folders}) != len(folders):
            raise LiveError(400, "文件夹名称不能重复。")
        verified = bool(not slot.accepts_pair(code) and device_key and device_key.matches(code))
        if join.bridge_id and join.bridge_contract != HUB_TOOL_CONTRACT_VERSION:
            raise LiveError(400, "设备工具契约版本不匹配。")
        try:
            token = slot.connect(
                code, join.name, folders, verified_key=verified, bridge_id=join.bridge_id,
                takeover_handle=join.takeover_handle,
            )
        except _DeviceOccupied as error:
            return _json({"error": str(error), "takeover_handle": error.handle}, error.status)
        reply: dict[str, Any] = {
            "device_token": token, "auth_kind": "device_key" if verified else "pair_code"
        }
        if join.bridge_id:
            reply["bridge_contract"] = HUB_TOOL_CONTRACT_VERSION
        return _json(reply)

    async def poll(request: Request) -> JSONResponse:
        return _json({"operation": await slot.next_operation(_bearer(request))})

    async def finish(request: Request) -> JSONResponse:
        token = _bearer(request)
        slot.heartbeat(token)
        result = await _body(request, _Result, MAX_BRIDGE_BODY)
        outputs = [value for value in (result.text, result.tool_result, result.error) if value is not None]
        if len(outputs) != 1:
            raise LiveError(400, "必须提供一种结果。")
        content = result.tool_result if result.tool_result is not None else result.text
        slot.finish(token, result.id, content, result.error)
        return _json({"ok": True})

    async def heartbeat(request: Request) -> JSONResponse:
        slot.heartbeat(_bearer(request))
        return _json({"ok": True})

    async def disconnect(request: Request) -> JSONResponse:
        slot.disconnect(_bearer(request))
        return _json({"ok": True})

    async def handle_error(_: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, LiveError):
            raise error
        return _json({"error": str(error)}, error.status)

    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        transport_security=build_transport_security(public_hostname),
    )
    app.add_middleware(_AuthorizeMcp, access_token=access_token, oauth=oauth)
    if oauth:
        app.routes.extend(oauth.routes())
    app.add_exception_handler(LiveError, handle_error)
    app.routes.extend(
        [
            Route("/health", health),
            Route("/live/pair", pair, methods=["POST"]),
            Route("/live/revoke", revoke, methods=["POST"]),
            Route("/live/connect", connect, methods=["POST"]),
            Route("/live/next", poll),
            Route("/live/heartbeat", heartbeat, methods=["POST"]),
            Route("/live/result", finish, methods=["POST"]),
            Route("/live/disconnect", disconnect, methods=["POST"]),
        ]
    )
    if download_file is not None:
        install_download(app, download_file)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated MCP Live relay.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-hostname", default="")
    args = parser.parse_args(argv)
    try:
        oauth = None
        approval_token = os.environ.get("MCP_LIVE_OAUTH_APPROVAL_TOKEN", "")
        if approval_token:
            if not args.public_hostname:
                raise ValueError("启用 OAuth 时必须指定公网域名。")
            oauth = LiveOAuth(f"https://{args.public_hostname}", approval_token)
        verifier = os.environ.get("MCP_LIVE_DEVICE_KEY_VERIFIER", "")
        download = os.environ.get("MCP_LIVE_DOWNLOAD_FILE", "")
        app = create_app(
            os.environ.get("MCP_LIVE_OWNER_TOKEN", ""),
            os.environ.get("MCP_LIVE_ACCESS_TOKEN", ""),
            args.public_hostname,
            oauth=oauth,
            device_key=DeviceKey(verifier) if verifier else None,
            download_file=Path(download) if download else None,
        )
    except ValueError as error:
        parser.error(str(error))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
