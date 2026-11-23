"""Connect a selected read-only workspace to the independent MCP Live relay."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import httpx

from .hub_tool_contract import HUB_TOOL_CONTRACT_VERSION
from .live_bridge import BridgeWorker
from .live_protocol import MAX_FOLDERS, POLL_SECONDS, TEXT_LIMIT, folder_alias, folder_path
from .models import PermissionMode
from .permissions import PermissionError as WorkspacePermissionError
from .tools import LocalDevTools

FILE_BYTES = 8_192
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_RELAY_RETRY_DELAYS = (1, 2, 4, 5)
_RELAY_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
_LEASE_ENDED = "服务器已结束这次设备接入。请重新启动共享。"


def _service_url(endpoint: str) -> str:
    message = "请输入 MCP Live 服务的 HTTPS 根地址。本地验证可使用回环 HTTP 地址。"
    try:
        url = urlsplit(endpoint.strip())
        hostname = url.hostname
        port = url.port
    except ValueError:
        raise ValueError(message) from None
    secure = url.scheme == "https" or (url.scheme == "http" and hostname in LOOPBACK)
    if (
        not secure
        or not hostname
        or port == 0
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError(message)
    return url.geturl().rstrip("/")


def _pairing_code(value: str) -> str:
    code = value.strip()
    if not code or len(code) > 256 or not code.isascii() or not code.isprintable() or " " in code:
        raise ValueError("请输入设备密钥或一次性配对码。")
    return code


def _connection_error(error: httpx.HTTPStatusError) -> str:
    status = error.response.status_code
    if status == 401:
        return "设备密钥不正确或暂时受限，也可能是配对码已过期。"
    if status == 409:
        return "已有电脑在线，请先断开原电脑。"
    return f"接入失败（状态 {status}）。"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Cache-Control": "no-store"}


def _read_workspace_text(tools: LocalDevTools, path: str) -> str:
    normal = PurePosixPath(path.replace("\\", "/"))
    if (
        not normal.parts
        or normal.is_absolute()
        or any(part in {"..", "~"} or PureWindowsPath(part).is_reserved() for part in normal.parts)
        or any(character in path for character in ":\x00")
    ):
        raise ValueError("需要授权目录内的相对路径。")
    resolved = (tools.workspace / normal).resolve(strict=True).relative_to(tools.workspace)
    if any(
        part == ".ssh"
        or part.startswith((".env", "id_rsa", "id_ed25519"))
        or part.endswith((".pem", ".key"))
        for part in (component.lower() for component in resolved.parts)
    ):
        raise ValueError("文件路径受保护。")
    return tools.read_file(resolved.as_posix())[:TEXT_LIMIT]


async def _read(workspaces: dict[str, LocalDevTools], path: str) -> dict[str, str]:
    try:
        if len(workspaces) == 1:
            tools = next(iter(workspaces.values()))
        else:
            name, path = folder_path(path)
            tools = workspaces[name]
        return {"text": await asyncio.to_thread(_read_workspace_text, tools, path)}
    except (OSError, ValueError, RuntimeError, KeyError, WorkspacePermissionError):
        return {"error": "无法读取该文件。请确认它在授权目录内且符合读取限制。"}


async def _send_with_retry(
    request: Callable[[], Awaitable[httpx.Response]],
    operation: str,
    on_progress: Callable[[str], None] | None,
    *,
    busy_is_transient: bool = False,
) -> httpx.Response:
    for delay in (*_RELAY_RETRY_DELAYS, None):
        try:
            response = await request()
        except httpx.TransportError as exc:
            reason = type(exc).__name__
        else:
            status = response.status_code
            if status not in _RELAY_RETRY_STATUS and not (busy_is_transient and status == 409):
                return response
            reason = f"HTTP {status}"
        if delay is None:
            raise RuntimeError(f"与中转的连接未恢复（{operation} · {reason}）。请检查网络后重新启动共享。")
        if on_progress is not None:
            on_progress(f"中转{operation}暂时失败（{reason}），{delay} 秒后重试。")
        await asyncio.sleep(delay)
    raise AssertionError("relay retry exhausted")


async def _relay_request(
    request: Callable[[], Awaitable[httpx.Response]],
    stop: asyncio.Event,
    operation: str,
    on_progress: Callable[[str], None] | None,
    *,
    busy_is_transient: bool = False,
) -> httpx.Response | None:
    pending = asyncio.create_task(
        _send_with_retry(request, operation, on_progress, busy_is_transient=busy_is_transient)
    )
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((pending, stopped), return_when=asyncio.FIRST_COMPLETED)
        return None if stop.is_set() else pending.result()
    finally:
        for task in (pending, stopped):
            task.cancel()
        await asyncio.gather(pending, stopped, return_exceptions=True)


async def _heartbeat(
    client: httpx.AsyncClient, headers: dict[str, str], stop: asyncio.Event
) -> bool:
    while not stop.is_set():
        await asyncio.sleep(10)
        try:
            response = await client.post("/live/heartbeat", headers=headers, timeout=4)
        except httpx.TransportError:
            continue
        if response.status_code == 401:
            stop.set()
            return True
    return False


async def _bridge_call(
    bridge: BridgeWorker | None, tool: dict, stop: asyncio.Event
) -> dict:
    if bridge is None:
        return {"error": "当前设备 0.8.9 引擎未启动。"}
    call = asyncio.create_task(bridge.call(tool))
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((call, stopped), return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            return {}
        return {"tool_result": await call}
    except (OSError, httpx.HTTPError, RuntimeError, KeyError, ValueError):
        return {"error": "0.8.9 本机工具调用未完成，请检查工作区状态。"}
    finally:
        call.cancel()
        stopped.cancel()
        await asyncio.gather(call, stopped, return_exceptions=True)


async def serve_agent(
    endpoint: str,
    pair_code: str,
    name: str,
    workspace: Path | Mapping[str, Path],
    *,
    stop: asyncio.Event | None = None,
    on_connected: Callable[[], None] | None = None,
    on_device_key: Callable[[], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    bridge_permissions: dict[str, PermissionMode] | None = None,
    windows_enabled: bool = False,
) -> None:
    """Run until stopped, revoked, or the relay is unavailable."""
    folders = {"": workspace} if isinstance(workspace, Path) else dict(workspace)
    if not 1 <= len(folders) <= MAX_FOLDERS:
        raise ValueError("请选择 1 至 32 个文件夹。")
    advertised = [folder_alias(name) for name in folders if name]
    workspaces: dict[str, LocalDevTools] = {}
    for folder, path in folders.items():
        root = path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("请选择一个目录。")
        workspaces[folder.casefold()] = LocalDevTools(
            root, "read_only", execution_profile="safe", max_file_bytes=FILE_BYTES
        )
    if len(workspaces) != len(folders):
        raise ValueError("文件夹名称不能重复。")
    stopped = stop if stop is not None else asyncio.Event()
    async with AsyncExitStack() as resources:
        origin = _service_url(endpoint)
        bridge = None
        if bridge_permissions is not None:
            if on_progress:
                on_progress("启动本机 CodexPro 引擎。")
            bridge = BridgeWorker(folders, bridge_permissions, name, windows_enabled)
            resources.push_async_callback(bridge.close)
            starting = asyncio.create_task(bridge.start())
            stop_request = asyncio.create_task(stopped.wait())
            try:
                await asyncio.wait((starting, stop_request), return_when=asyncio.FIRST_COMPLETED)
                if stopped.is_set():
                    return
                await starting
                if on_progress:
                    on_progress("本机引擎已启动，Gateway 健康检查通过。")
                    if bridge.windows_warning:
                        on_progress(bridge.windows_warning)
            finally:
                if not starting.done():
                    starting.cancel()
                stop_request.cancel()
                await asyncio.gather(starting, stop_request, return_exceptions=True)
        if on_progress:
            on_progress("正在向公网中转注册本机。")
        client = await resources.enter_async_context(
            httpx.AsyncClient(base_url=origin, timeout=POLL_SECONDS + 3, trust_env=False)
        )
        join: dict[str, str | list[str] | int] = {"name": name}
        if advertised:
            join["folders"] = advertised
        if bridge is not None:
            join["bridge_id"] = bridge.id
            join["bridge_contract"] = HUB_TOOL_CONTRACT_VERSION
        response = await client.post("/live/connect", headers=_headers(pair_code), json=join)
        response.raise_for_status()
        connected = response.json()
        headers = _headers(connected["device_token"])
        heartbeat = None
        try:
            if bridge is not None and connected.get("bridge_contract") != HUB_TOOL_CONTRACT_VERSION:
                raise RuntimeError("服务器尚未启用相同的 0.8.9 工具契约。")
            heartbeat = asyncio.create_task(_heartbeat(client, headers, stopped))
            if on_device_key is not None and connected.get("auth_kind") == "device_key":
                on_device_key()
            if on_connected is not None and not stopped.is_set():
                on_connected()
            while not stopped.is_set():
                reply = await _relay_request(
                    lambda: client.get("/live/next", headers=headers), stopped, "轮询", on_progress,
                    busy_is_transient=True,
                )
                if reply is None:
                    break
                if reply.status_code == 401:
                    raise RuntimeError(_LEASE_ENDED)
                reply.raise_for_status()
                operation = reply.json()["operation"]
                if operation is None:
                    continue
                if "tool" in operation:
                    result = await _bridge_call(bridge, operation["tool"], stopped)
                    if stopped.is_set():
                        break
                else:
                    result = await _read(workspaces, operation["path"])
                submitted = await _relay_request(
                    partial(client.post, "/live/result", headers=headers, json={"id": operation["id"], **result}),
                    stopped, "提交结果", on_progress,
                )
                if submitted is None:
                    break
                if submitted.status_code == 401:
                    raise RuntimeError(_LEASE_ENDED)
                if submitted.status_code != 409:
                    submitted.raise_for_status()
            if heartbeat is not None and heartbeat.done() and heartbeat.result():
                raise RuntimeError(_LEASE_ENDED)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            with suppress(httpx.TransportError):
                await client.post("/live/disconnect", headers=headers, timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect a temporary read-only MCP workspace.")
    parser.add_argument("endpoint", help="MCP Live root address, without /mcp")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--name", default=socket.gethostname())
    args = parser.parse_args(argv)
    try:
        endpoint = _service_url(args.endpoint)
        if sys.stdin.isatty():
            code = getpass.getpass("Device key or pair code: ").strip()
        else:
            code = sys.stdin.readline(257).strip()
        asyncio.run(serve_agent(endpoint, _pairing_code(code), args.name, args.workspace))
    except KeyboardInterrupt:
        return 0
    except httpx.HTTPStatusError as error:
        print(_connection_error(error), file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, httpx.TransportError, ValueError):
        print("接入失败，请检查服务地址、网络和工作目录。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
