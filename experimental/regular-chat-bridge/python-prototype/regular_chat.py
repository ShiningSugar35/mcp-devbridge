"""Optional Regular Chat browser-controller sidecar integration.

The controller is deliberately isolated from the MCP Gateway.  It owns a
DevBridge-specific browser profile and communicates with the Python desktop
process over newline-delimited JSON on stdio; no local TCP listener or auth
secret is introduced.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from . import constants
from .engines import find_node
from .platform_support import popen_platform_kwargs, run_platform_kwargs

REGULAR_CHAT_DIRNAME = "regular-chat"
DEFAULT_PROFILE_ID = "default-managed"
SUPPORTED_ENGINES = {"managed-chromium", "msedge", "chrome"}
MAX_RPC_LINE_BYTES = 1024 * 1024


class RegularChatError(RuntimeError):
    """Controller lifecycle or protocol failure."""


@dataclass(frozen=True)
class RegularChatPaths:
    runtime_root: Path
    controller_entry: Path
    package_root: Path
    browsers_dir: Path


def regular_chat_runtime_dir() -> Path:
    return constants.config_dir() / REGULAR_CHAT_DIRNAME


def workspace_hash(workspace: str | Path) -> str:
    canonical = str(Path(workspace).expanduser().resolve())
    if os.name == "nt":
        canonical = os.path.normcase(canonical)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _candidate_package_roots() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("REGULAR_CHAT_CONTROLLER_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "third_party" / "regular-chat-controller")
        candidates.append(
            Path(sys.executable).resolve().parent.parent
            / "third_party"
            / "regular-chat-controller"
        )
    candidates.append(Path(__file__).resolve().parents[2] / "third_party" / "regular-chat-controller")
    seen: set[Path] = set()
    result: list[Path] = []
    for item in candidates:
        resolved = item.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def resolve_regular_chat_paths() -> RegularChatPaths:
    runtime_root = regular_chat_runtime_dir().resolve()
    for package_root in _candidate_package_roots():
        entry = package_root / "dist" / "src" / "stdioMain.js"
        if entry.is_file():
            return RegularChatPaths(
                runtime_root=runtime_root,
                controller_entry=entry,
                package_root=package_root,
                browsers_dir=runtime_root / "browsers",
            )
    first = _candidate_package_roots()[0]
    return RegularChatPaths(
        runtime_root=runtime_root,
        controller_entry=first / "dist" / "src" / "stdioMain.js",
        package_root=first,
        browsers_dir=runtime_root / "browsers",
    )


def _managed_browser_executable(package_root: Path, browsers_dir: Path) -> Path | None:
    cli = package_root / "node_modules" / "playwright" / "cli.js"
    if not cli.is_file():
        return None
    node = find_node()
    if not node:
        return None
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    probe = subprocess.run(
        [node, str(cli), "install", "--dry-run", "chromium"],
        cwd=str(package_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
        **run_platform_kwargs(),
    )
    if probe.returncode != 0:
        return None
    for line in probe.stdout.splitlines():
        marker = "Install location:"
        if marker in line:
            location = Path(line.split(marker, 1)[1].strip())
            if location.is_dir():
                for candidate in location.rglob("chrome.exe" if os.name == "nt" else "chrome"):
                    if candidate.is_file():
                        return candidate
    return None


def managed_browser_ready() -> bool:
    paths = resolve_regular_chat_paths()
    return _managed_browser_executable(paths.package_root, paths.browsers_dir) is not None


def install_managed_browser(timeout_seconds: float = 900.0) -> dict[str, Any]:
    """Install the Playwright-pinned Chromium into DevBridge's private cache."""
    paths = resolve_regular_chat_paths()
    node = find_node()
    cli = paths.package_root / "node_modules" / "playwright" / "cli.js"
    if not node:
        raise RegularChatError("未找到内置 Node.js 运行组件。")
    if not cli.is_file():
        raise RegularChatError("Regular Chat 运行组件不完整，请重新安装 MCP DevBridge。")
    paths.browsers_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(paths.browsers_dir)
    result = subprocess.run(
        [node, str(cli), "install", "chromium"],
        cwd=str(paths.package_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        **run_platform_kwargs(),
    )
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-20:])
        raise RegularChatError(f"独立浏览器安装失败（exit={result.returncode}）。\n{tail}")
    return {"ok": True, "browser_ready": managed_browser_ready(), "runtime_root": str(paths.runtime_root)}


class RegularChatClient:
    """Own exactly one stdio sidecar and serialize RPC calls to it."""

    def __init__(
        self,
        *,
        engine: str = "managed-chromium",
        profile_id: str = DEFAULT_PROFILE_ID,
        headed: bool = True,
        paths: RegularChatPaths | None = None,
    ) -> None:
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(f"unsupported Regular Chat engine: {engine}")
        self.engine = engine
        self.profile_id = profile_id
        self.headed = headed
        self.paths = paths or resolve_regular_chat_paths()
        self._proc: subprocess.Popen[str] | None = None
        self._rpc_lock = threading.RLock()
        self._next_id = 1
        self._responses: queue.Queue[str | None] = queue.Queue(maxsize=64)
        self._stderr_tail: deque[str] = deque(maxlen=64)
        self._reader_threads: list[threading.Thread] = []

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_running:
            return
        node = find_node()
        if not node:
            raise RegularChatError("未找到内置 Node.js 运行组件。")
        if not self.paths.controller_entry.is_file():
            raise RegularChatError(
                f"Regular Chat Controller 构建产物缺失：{self.paths.controller_entry}"
            )
        self.paths.runtime_root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "REGULAR_CHAT_RUNTIME_ROOT": str(self.paths.runtime_root),
                "REGULAR_CHAT_PROFILE_ID": self.profile_id,
                "REGULAR_CHAT_BROWSER_ENGINE": self.engine,
                "REGULAR_CHAT_HEADED": "1" if self.headed else "0",
                "PLAYWRIGHT_BROWSERS_PATH": str(self.paths.browsers_dir),
            }
        )
        self._proc = subprocess.Popen(
            [node, str(self.paths.controller_entry)],
            cwd=str(self.paths.package_root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_platform_kwargs(new_session=True),
        )
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            self.stop(force=True)
            raise RegularChatError("Regular Chat Controller stdio 初始化失败。")
        self._responses = queue.Queue(maxsize=64)
        self._stderr_tail.clear()
        self._reader_threads = [
            threading.Thread(
                target=self._stdout_reader,
                args=(self._proc, self._responses),
                name="regular-chat-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._stderr_reader,
                args=(self._proc,),
                name="regular-chat-stderr",
                daemon=True,
            ),
        ]
        for thread in self._reader_threads:
            thread.start()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> Any:
        if not 0.1 <= timeout_seconds <= 900.0:
            raise ValueError("timeout_seconds must be between 0.1 and 900")
        with self._rpc_lock:
            self.start()
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            if proc.poll() is not None:
                raise RegularChatError(self._terminal_error("Regular Chat Controller 已退出"))
            request_id = self._next_id
            self._next_id += 1
            payload = json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(payload.encode("utf-8")) > MAX_RPC_LINE_BYTES:
                raise RegularChatError("Regular Chat 请求超过本地 IPC 大小限制。")
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RegularChatError(self._terminal_error("Regular Chat Controller 通信中断")) from exc
            try:
                line = self._responses.get(timeout=timeout_seconds)
            except queue.Empty as exc:
                self.stop(force=True)
                raise RegularChatError(
                    f"Regular Chat Controller 在 {timeout_seconds:.1f} 秒内未返回，已终止该 sidecar；durable 会话状态保留。"
                ) from exc
            if line is None:
                raise RegularChatError(self._terminal_error("Regular Chat Controller 未返回结果"))
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self.stop(force=True)
                raise RegularChatError("Regular Chat Controller 返回了无效响应，已安全停止该 sidecar。") from exc
            if response.get("id") != request_id:
                self.stop(force=True)
                raise RegularChatError("Regular Chat Controller 响应顺序异常，已安全停止本次操作。")
            if "error" in response:
                message = str((response.get("error") or {}).get("message") or "未知错误")
                raise RegularChatError(message)
            return response.get("result")

    def abort(self) -> None:
        """Interrupt an in-flight RPC and terminate only this client's owned process tree.

        Unlike graceful ``stop()``, this method deliberately does not wait for the
        serialized RPC lock. It is used during desktop shutdown/cancellation so a
        long ``turn.watch`` cannot hold the application open for its full budget.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        self._terminate_owned_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        for thread in list(self._reader_threads):
            thread.join(timeout=0.5)
        self._reader_threads.clear()
        with contextlib.suppress(queue.Full):
            self._responses.put_nowait(None)

    def stop(self, *, force: bool = False) -> None:
        with self._rpc_lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            if not force and proc.poll() is None:
                try:
                    if proc.stdin is not None:
                        stop_id = self._next_id
                        self._next_id += 1
                        proc.stdin.write(
                            json.dumps({"id": stop_id, "method": "controller.stop", "params": {}})
                            + "\n"
                        )
                        proc.stdin.flush()
                        with contextlib.suppress(queue.Empty):
                            self._responses.get(timeout=2.0)
                except OSError:
                    pass
            self._terminate_owned_tree(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
            for thread in self._reader_threads:
                thread.join(timeout=0.5)
            self._reader_threads.clear()

    @staticmethod
    def _stdout_reader(proc: subprocess.Popen[str], responses: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if len(line.encode("utf-8", errors="replace")) > MAX_RPC_LINE_BYTES:
                    with contextlib.suppress(queue.Full):
                        responses.put_nowait(None)
                    with contextlib.suppress(Exception):
                        proc.terminate()
                    return
                try:
                    responses.put(line, timeout=1.0)
                except queue.Full:
                    with contextlib.suppress(Exception):
                        proc.terminate()
                    return
        finally:
            with contextlib.suppress(queue.Full):
                responses.put_nowait(None)

    def _stderr_reader(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(OSError):
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip()[-2000:])

    @staticmethod
    def _terminate_owned_tree(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        processes: list[psutil.Process] = []
        with contextlib.suppress(psutil.Error):
            parent = psutil.Process(proc.pid)
            processes = [*parent.children(recursive=True), parent]
            for process in reversed(processes):
                with contextlib.suppress(psutil.Error):
                    process.terminate()
            _, alive = psutil.wait_procs(processes, timeout=3.0)
            for process in alive:
                with contextlib.suppress(psutil.Error):
                    process.kill()
            psutil.wait_procs(alive, timeout=2.0)
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=2.0)

    def _terminal_error(self, prefix: str) -> str:
        detail = "\n".join(self._stderr_tail).strip()
        return f"{prefix}。{detail[-1000:]}" if detail else f"{prefix}。"


def reset_profile(engine: str = "managed-chromium", profile_id: str = DEFAULT_PROFILE_ID) -> Path:
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"unsupported Regular Chat engine: {engine}")
    suffix = "managed" if engine == "managed-chromium" else engine
    target = regular_chat_runtime_dir() / "profiles" / f"{profile_id}-{suffix}"
    if target.exists():
        shutil.rmtree(target)
    return target
