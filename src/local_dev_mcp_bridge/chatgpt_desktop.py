"""Experimental bridge for ordinary ChatGPT Desktop chats as MCP-backed workers.

The bridge intentionally does not call private ChatGPT HTTP APIs or copy account
credentials. It uses the Codex/ChatGPT Desktop deep-link surface to create an
ordinary ``mode=chat`` conversation and Chromium DevTools Protocol (CDP) on a
loopback-only debugging port to click the real Send button. The child chat does
its local work through the user's already-connected MCP DevBridge app.

CDP is powerful. It is therefore opt-in: a small state file is written only
after ``prepare_chatgpt_bridge`` is explicitly requested. The debugging port is
bound to 127.0.0.1 and the bridge can restore a normal Desktop launch later.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil

from . import constants
from .platform_support import IS_WINDOWS, popen_platform_kwargs

_DEFAULT_DEBUG_PORT = 19222
_DEFAULT_TASK_TIMEOUT = 15 * 60
_STATE_NAME = "chatgpt-desktop-bridge.json"
_RECEIPT_DIR = ".mcp-devbridge-chat-agent-receipts"


@dataclass
class ChatGPTBridgeState:
    enabled: bool = False
    debug_port: int = _DEFAULT_DEBUG_PORT
    executable: str = ""
    prepared_at: float = 0.0


def _state_path() -> Path:
    return constants.config_dir() / _STATE_NAME


def _read_state() -> ChatGPTBridgeState:
    path = _state_path()
    if not path.is_file():
        return ChatGPTBridgeState()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return ChatGPTBridgeState(
            enabled=bool(value.get("enabled", False)),
            debug_port=int(value.get("debug_port") or _DEFAULT_DEBUG_PORT),
            executable=str(value.get("executable") or ""),
            prepared_at=float(value.get("prepared_at") or 0.0),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ChatGPTBridgeState()


def _write_state(state: ChatGPTBridgeState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _candidate_chatgpt_executables() -> list[Path]:
    values: list[Path] = []
    explicit = os.environ.get("MCP_DEVBRIDGE_CHATGPT_EXE", "").strip()
    if explicit:
        values.append(Path(explicit))
    state = _read_state()
    if state.executable:
        values.append(Path(state.executable))
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            if str(proc.info.get("name") or "").casefold() != "chatgpt.exe":
                continue
            raw = str(proc.info.get("exe") or "")
            if raw:
                values.append(Path(raw))
    except (psutil.Error, OSError):
        pass
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if str(local):
        values.extend(
            [
                local / "Programs" / "ChatGPT" / "ChatGPT.exe",
                local / "Programs" / "Codex" / "ChatGPT.exe",
            ]
        )
    # Common portable/custom install locations. These are probes only.
    for drive in ("C:", "D:", "E:", "F:"):
        values.extend(
            [
                Path(drive + "\\软件\\Codex\\ChatGPT.exe"),
                Path(drive + "\\Software\\Codex\\ChatGPT.exe"),
            ]
        )
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            candidate = value.expanduser().resolve()
        except OSError:
            continue
        key = str(candidate).casefold()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        result.append(candidate)
    return result


def find_chatgpt_executable() -> str:
    candidates = _candidate_chatgpt_executables()
    return str(candidates[0]) if candidates else ""


def _cdp_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{int(port)}{path}"


def _list_cdp_targets(port: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    try:
        response = httpx.get(_cdp_url(port, "/json/list"), timeout=timeout)
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _cdp_ready(port: int) -> bool:
    return any(item.get("webSocketDebuggerUrl") for item in _list_cdp_targets(port))


def _free_loopback_port(preferred: int = _DEFAULT_DEBUG_PORT) -> int:
    if preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", preferred))
            except OSError:
                pass
            else:
                return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _stop_chatgpt_processes(executable: Path) -> None:
    targets: list[psutil.Process] = []
    wanted = str(executable.resolve()).casefold()
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            if str(proc.info.get("name") or "").casefold() != "chatgpt.exe":
                continue
            raw = str(proc.info.get("exe") or "")
            if raw and str(Path(raw).resolve()).casefold() == wanted:
                targets.append(proc)
    except (psutil.Error, OSError):
        return
    for proc in sorted(targets, key=lambda item: item.pid, reverse=True):
        with suppress(psutil.Error):
            proc.kill()
    if targets:
        psutil.wait_procs(targets, timeout=5)


def prepare_chatgpt_bridge(*, restart: bool = True, debug_port: int = 0) -> dict[str, Any]:
    """Explicitly prepare ChatGPT Desktop for the ordinary-Chat worker bridge."""
    if not IS_WINDOWS:
        raise RuntimeError("ChatGPT Desktop Chat Agent bridge currently supports Windows only.")
    executable_text = find_chatgpt_executable()
    if not executable_text:
        raise RuntimeError("未找到 ChatGPT/Codex Desktop 的 ChatGPT.exe。")
    executable = Path(executable_text)
    state = _read_state()
    existing_port = int(debug_port or state.debug_port or _DEFAULT_DEBUG_PORT)
    if _cdp_ready(existing_port):
        prepared = ChatGPTBridgeState(True, existing_port, str(executable), time.time())
        _write_state(prepared)
        return bridge_status()
    if not restart:
        raise RuntimeError("ChatGPT Desktop 未开启 CDP；需要显式允许重启后才能准备 Chat Agent bridge。")
    port = _free_loopback_port(int(debug_port or _DEFAULT_DEBUG_PORT))
    _stop_chatgpt_processes(executable)
    subprocess.Popen(
        [
            str(executable),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ],
        cwd=str(executable.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_platform_kwargs(new_session=True),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _cdp_ready(port):
            prepared = ChatGPTBridgeState(True, port, str(executable), time.time())
            _write_state(prepared)
            return bridge_status()
        time.sleep(0.2)
    raise RuntimeError("ChatGPT Desktop 已重启，但 15 秒内未出现 loopback CDP 端口。")


def restore_normal_chatgpt_launch() -> dict[str, Any]:
    """Disable the bridge and relaunch ChatGPT Desktop without CDP flags."""
    state = _read_state()
    executable_text = state.executable or find_chatgpt_executable()
    if not executable_text:
        _write_state(ChatGPTBridgeState(enabled=False))
        return bridge_status()
    executable = Path(executable_text)
    _stop_chatgpt_processes(executable)
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_platform_kwargs(new_session=True),
    )
    _write_state(ChatGPTBridgeState(False, state.debug_port, str(executable), time.time()))
    return bridge_status()


def bridge_status() -> dict[str, Any]:
    state = _read_state()
    executable = state.executable or find_chatgpt_executable()
    ready = bool(state.enabled and _cdp_ready(state.debug_port))
    return {
        "supported": IS_WINDOWS,
        "enabled": state.enabled,
        "ready": ready,
        "debug_port": state.debug_port,
        "loopback_only": True,
        "executable": executable,
        "mode": "ordinary_chat",
        "uses_work_or_codex": False,
        "requires_connected_mcp_devbridge_app": True,
        "note": (
            "Ordinary Chat is launched via codex:// mode=chat and works through the user's connected "
            "MCP DevBridge app. CDP is used only to submit/observe the Desktop UI; it is never exposed publicly."
        ),
    }


class _CdpSocket:
    """Tiny RFC6455 client sufficient for loopback Chrome DevTools Protocol."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("CDP WebSocket must be loopback ws:// only.")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        self.socket = socket.create_connection((self.host, self.port), timeout=timeout)
        self.socket.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: websocket\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        headers = bytearray()
        while b"\r\n\r\n" not in headers:
            chunk = self.socket.recv(4096)
            if not chunk:
                break
            headers.extend(chunk)
            if len(headers) > 64_000:
                break
        first = bytes(headers).split(b"\r\n", 1)[0]
        if b" 101 " not in first:
            self.close()
            raise RuntimeError(f"CDP WebSocket upgrade failed: {first.decode('ascii', errors='replace')}")
        self._next_id = 0

    def close(self) -> None:
        with suppress(OSError):
            self.socket.close()

    def __enter__(self) -> _CdpSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(bytes(header) + masked)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise ConnectionError("CDP WebSocket closed unexpectedly.")
            data.extend(chunk)
        return bytes(data)

    def _recv_message(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("CDP WebSocket closed.")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode in {0x1, 0x0}:
                fragments.extend(payload)
                if final:
                    return fragments.decode("utf-8", errors="replace")

    def _send_pong(self, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        header = bytearray([0x8A, 0x80 | len(payload)])
        header.extend(mask)
        header.extend(bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))
        self.socket.sendall(bytes(header))

    def evaluate(self, expression: str) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._send_text(
            json.dumps(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
                },
                ensure_ascii=False,
            )
        )
        while True:
            message = json.loads(self._recv_message())
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"CDP Runtime.evaluate failed: {message['error']}")
            result = message.get("result", {}).get("result", {})
            if result.get("exceptionDetails"):
                raise RuntimeError(f"CDP expression failed: {result.get('exceptionDetails')}")
            return result.get("value")


class ChatGPTDesktopBridge:
    """Launch and monitor ordinary ChatGPT chats backed by MCP DevBridge tools."""

    _submit_lock = threading.Lock()

    def __init__(self) -> None:
        self.state = _read_state()

    @property
    def ready(self) -> bool:
        self.state = _read_state()
        return bool(self.state.enabled and _cdp_ready(self.state.debug_port))

    def capabilities(self) -> dict[str, Any]:
        return bridge_status()

    def _main_target(self) -> dict[str, Any]:
        targets = _list_cdp_targets(self.state.debug_port, timeout=3.0)
        preferred = next(
            (item for item in targets if item.get("type") == "page" and item.get("url") == "app://-/index.html"),
            None,
        )
        if preferred is None:
            preferred = next(
                (
                    item
                    for item in targets
                    if item.get("type") == "page" and "avatar-overlay" not in str(item.get("url") or "")
                ),
                None,
            )
        if preferred is None or not preferred.get("webSocketDebuggerUrl"):
            raise RuntimeError("找不到 ChatGPT Desktop 主 renderer。")
        return preferred

    def _evaluate(self, expression: str) -> Any:
        target = self._main_target()
        with _CdpSocket(str(target["webSocketDebuggerUrl"]), timeout=6.0) as cdp:
            return cdp.evaluate(expression)

    def _launch_and_send(self, prompt: str, task_id: str) -> str:
        executable = self.state.executable or find_chatgpt_executable()
        if not executable:
            raise RuntimeError("ChatGPT Desktop executable is unavailable.")
        url = "codex://new?" + urllib.parse.urlencode({"mode": "chat", "prompt": prompt})
        with self._submit_lock:
            subprocess.Popen(
                [executable, url],
                cwd=str(Path(executable).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_platform_kwargs(new_session=True),
            )
            deadline = time.monotonic() + 8
            token = f"MCP_CHILD_TASK_ID={task_id}"
            while time.monotonic() < deadline:
                state = self._evaluate(
                    "(() => {"
                    "const box=document.querySelector('[role=\"textbox\"][aria-label=\"给 ChatGPT 发消息\"]');"
                    "const send=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='发送');"
                    "return {text:box?.innerText||'',ready:!!send&&!send.disabled};"
                    "})()"
                )
                if isinstance(state, dict) and token in str(state.get("text") or "") and state.get("ready"):
                    clicked = self._evaluate(
                        "(() => {"
                        "const send=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='发送');"
                        "if(!send||send.disabled)return false;send.click();return true;"
                        "})()"
                    )
                    if not clicked:
                        raise RuntimeError("ChatGPT Desktop Send button was not clickable.")
                    break
                time.sleep(0.12)
            else:
                raise RuntimeError("ChatGPT Desktop composer did not receive the managed task within 8 seconds.")
            conversation_id = ""
            id_deadline = time.monotonic() + 4
            while time.monotonic() < id_deadline:
                value = self._evaluate(
                    "(() => document.querySelector('[data-above-composer-conversation-id]')"
                    "?.getAttribute('data-above-composer-conversation-id')||'')()"
                )
                if isinstance(value, str) and value.startswith("chatgpt:"):
                    conversation_id = value
                    break
                time.sleep(0.12)
            return conversation_id

    def stop_conversation(self, conversation_id: str) -> bool:
        """Best-effort stop for one managed ordinary-Chat turn via its sidebar key."""
        conversation_id = conversation_id.strip()
        if not conversation_id or not self.ready:
            return False
        encoded = json.dumps(conversation_id)
        with self._submit_lock:
            opened = self._evaluate(
                "(() => {"
                f"const id={encoded};"
                "const item=[...document.querySelectorAll('[data-sidebar-chatgpt-conversation-key]')]"
                ".find(e=>e.getAttribute('data-sidebar-chatgpt-conversation-key')===id);"
                "if(!item)return false;item.click();return true;"
                "})()"
            )
            if not opened:
                return False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                stopped = self._evaluate(
                    "(() => {"
                    "const button=[...document.querySelectorAll('button')].find(b=>"
                    "['停止','停止生成'].includes(b.getAttribute('aria-label')||'')||"
                    "['停止','停止生成'].includes((b.innerText||'').trim()));"
                    "if(!button||button.disabled)return false;button.click();return true;"
                    "})()"
                )
                if stopped:
                    return True
                time.sleep(0.12)
        return False

    @staticmethod
    def _relative_to_route(route_root: Path, target: Path) -> str:
        try:
            relative = target.resolve().relative_to(route_root.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"ChatGPT Chat executor target must stay under the routed MCP root: {route_root}"
            ) from exc
        value = relative.as_posix()
        return value if value not in {"", "."} else "."

    def run_task(
        self,
        *,
        task_id: str,
        assignment: str,
        route_root: Path,
        target_workspace: Path,
        write: bool,
        route_workspace_id: str = "",
        timeout_seconds: int | None = None,
        on_started: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], str, Path]:
        if not self.ready:
            raise RuntimeError(
                "ChatGPT Desktop Chat Agent bridge is not ready. Prepare it explicitly from MCP DevBridge first."
            )
        route_root = route_root.resolve()
        target_workspace = target_workspace.resolve()
        target_relative = self._relative_to_route(route_root, target_workspace)
        receipt_dir = route_root / _RECEIPT_DIR
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipt_dir / f"{task_id}.json"
        receipt.unlink(missing_ok=True)
        receipt_relative = receipt.relative_to(route_root).as_posix()
        root_display = route_root.as_posix()
        mode_note = "You may modify files inside the target workspace." if write else "Do not modify target workspace files."
        route_note = (
            f" Every MCP tool call must include devbridge_workspace_id={route_workspace_id!r}; keep this same value for the entire task."
            if route_workspace_id
            else ""
        )
        prompt = (
            "You are a managed child development agent running in ordinary ChatGPT Chat mode. "
            "Stay in Chat mode; DO NOT hand off to Work or Codex. Use the connected MCP DevBridge/mcp-wjp tools directly. "
            "Do NOT call open_workspace, switch_workspace, or any workspace-changing tool. "
            f"The MCP service root is {root_display}. Every file path and command cwd you send to MCP must be relative to that root."
            f"{route_note} "
            f"Your target workspace is exactly {target_relative}. Work only inside that target, except for the infrastructure receipt path below. "
            f"{mode_note} Do not retry the assignment against a different path or workspace. "
            "Actually execute the requested work and verify important changes/tests with MCP tools; never claim success from reasoning alone. "
            f"MCP_CHILD_TASK_ID={task_id}\n\n"
            f"ASSIGNMENT:\n{assignment.strip()}\n\n"
            "COMPLETION CONTRACT: after the assignment is genuinely complete, your final MCP action must write a UTF-8 JSON object "
            f"to the exact D-root-relative receipt path {receipt_relative}. The JSON must include "
            f"{{\"task_id\":\"{task_id}\",\"status\":\"success\",\"summary\":\"short factual result\"}}; "
            "use status=failed with an error field if the assignment was not completed. Then read the same receipt path back once. "
            "Do not write the receipt anywhere else and do not continue retrying after it has been verified."
        )
        conversation_id = self._launch_and_send(prompt, task_id)
        if conversation_id and on_started is not None:
            on_started(conversation_id)
        deadline = time.monotonic() + int(
            timeout_seconds
            or os.environ.get("MCP_DEVBRIDGE_CHATGPT_TASK_TIMEOUT", _DEFAULT_TASK_TIMEOUT)
        )
        while time.monotonic() < deadline:
            if receipt.is_file():
                try:
                    value = json.loads(receipt.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.2)
                    continue
                if not isinstance(value, dict) or str(value.get("task_id") or "") != task_id:
                    time.sleep(0.2)
                    continue
                return value, conversation_id, receipt
            time.sleep(0.25)
        raise RuntimeError(
            f"ChatGPT Chat Agent timed out after {int(timeout_seconds or _DEFAULT_TASK_TIMEOUT)} seconds without a verified receipt."
        )
