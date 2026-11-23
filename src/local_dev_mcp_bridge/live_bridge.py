"""Transport to an isolated, original 0.8.9 DevBridge worker."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

from .models import PermissionMode


class BridgeWorker:
    def __init__(self, projects: dict[str, Path], permissions: dict[str, PermissionMode],
                 name: str, windows_enabled: bool) -> None:
        self.id = secrets.token_hex(16)
        self._token = secrets.token_urlsafe(32)
        self._windows_enabled = windows_enabled and any(mode != "read_only" for mode in permissions.values())
        self._configuration = {
            "device_id": self.id,
            "gateway_token": self._token,
            "device_name": name,
            "windows_enabled": windows_enabled,
            "projects": [
                {"name": alias, "root": str(path), "mode": permissions.get(alias, "read_only")}
                for alias, path in projects.items()
            ],
        }
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._settings: TemporaryDirectory[str] | None = None

    async def start(self) -> None:
        self._settings = TemporaryDirectory(prefix="mcp-live-runtime-")
        command = (
            [str(Path(sys.executable).with_name("MCP-Live-Agent.exe")), "--live-bridge-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-B", "-m", "local_dev_mcp_bridge.live_bridge_worker"]
        )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            env = {
                **os.environ,
                "LOCALDEV_MCP_CONFIG_DIR": self._settings.name,
                "PYTHONIOENCODING": "utf-8",
            }
            if os.name == "nt" and self._windows_enabled:
                from .live_windows_runtime import windows_uvx

                env["MCPDEVBRIDGE_UVX_EXE"] = await windows_uvx()
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=flags,
                env=env,
            )
            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin.write(json.dumps(self._configuration).encode() + b"\n")
            await self._process.stdin.drain()
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), 120)
                result = json.loads(line)
            except TimeoutError:
                raise RuntimeError("本机引擎启动超时（等待 120 秒）。请查看测试与日志。") from None
            except ValueError:
                raise RuntimeError("本机引擎未返回有效启动状态。请查看测试与日志。") from None
            finally:
                self._configuration.clear()
            port = result.get("port")
            if not isinstance(port, int) or not 0 < port < 65536:
                raise RuntimeError(result.get("error") or "0.8.9 本机工具引擎启动失败。")
            self._client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                timeout=130,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            for _ in range(60):
                try:
                    response = await self._client.get("/health", timeout=0.3)
                    if response.is_success:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            raise RuntimeError("0.8.9 本机 Gateway 未监听。")
        except BaseException:
            await self.close()
            raise

    async def call(self, tool: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("0.8.9 本机工具引擎未运行。")
        response = await self._client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": tool},
        )
        response.raise_for_status()
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    message = json.loads(line.removeprefix("data:").strip())
                    if message.get("id") == 1:
                        return self._result(message)
            raise ValueError("0.8.9 Gateway 未返回当前工具的结果。")
        return self._result(response.json())

    @staticmethod
    def _result(message: dict[str, Any]) -> dict[str, Any]:
        if "result" in message:
            return message["result"]
        error = message.get("error") or {}
        return {
            "content": [{"type": "text", "text": str(error.get("message", "工具执行失败。"))}],
            "isError": True,
            "structuredContent": error.get("data"),
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await self._process.stdin.wait_closed()
            try:
                await asyncio.wait_for(self._process.wait(), 40)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._process.terminate()
                await self._process.wait()
            if self._process.stdout is not None:
                await self._process.stdout.read()
            self._process = None
        if self._settings is not None:
            self._settings.cleanup()
            self._settings = None
