"""Run the original DevBridge engines and local Gateway for one Live session."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
from contextlib import AbstractContextManager, ExitStack, nullcontext, suppress
from pathlib import Path

from .config_store import save_projects
from .device_hub import DeviceRegistry
from .engines import EngineState
from .gateway import OAuthGateway
from .live_elevation import live_admin_session as _live_admin_session
from .models import PermissionMode, ProjectConfig
from .platform_support import IS_WINDOWS
from .project_manager import ProjectManager


def _ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket()
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


def _send_startup(message: dict[str, int | str]) -> None:
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _startup_elevation(modes: list[PermissionMode]) -> AbstractContextManager[None]:
    if IS_WINDOWS and "system" in modes:
        return _live_admin_session()
    return nullcontext()


def main() -> int:
    if getattr(sys, "frozen", False):
        # PyInstaller exposes the bundle directory on the frozen interpreter.
        runtime = Path(sys._MEIPASS) / "third_party" / "codexpro" / "dist"  # pyright: ignore[reportAttributeAccessIssue]
    else:
        runtime = Path(__file__).resolve().parents[2] / "build" / "codexpro-runtime" / "dist"
    if (runtime / "http.js").is_file():
        os.environ["CODEXPRO_DIST_DIR"] = str(runtime)
    manager = ProjectManager()
    gateway: OAuthGateway | None = None
    closed = threading.Event()
    startup = ExitStack()

    def watch_parent() -> None:
        sys.stdin.read(1)
        closed.set()

    try:
        request = json.loads(sys.stdin.readline())
        # No other local process or previous portable session shares this catalog.
        threading.Thread(target=watch_parent, daemon=True, name="live-parent-pipe").start()
        options = request["projects"]
        ports = _ports(2 * len(options) + 1)
        token = request["gateway_token"]
        on_windows = IS_WINDOWS and bool(request.get("windows_enabled"))
        projects = [
            ProjectConfig(
                id=secrets.token_hex(8),
                display_name=item["name"],
                root_path=str(Path(item["root"]).resolve(strict=True)),
                permission_mode=item["mode"],
                codexpro_port=ports[i * 2 + 1],
                windows_bridge_port=ports[i * 2 + 2],
                windows_enabled=on_windows and item["mode"] != "read_only",
            )
            for i, item in enumerate(options)
        ]
        save_projects(projects)
        startup.enter_context(_startup_elevation([project.permission_mode for project in projects]))
        for project in projects:
            if closed.is_set():
                return 0
            elevated = IS_WINDOWS and project.permission_mode == "system"
            view = manager.start(
                project.id,
                codex_token=token,
                permission_mode=project.permission_mode,
                execution_profile="developer",
                windows_token=secrets.token_urlsafe(32) if project.windows_enabled else None,
                elevated=elevated,
            )
            if view.state != EngineState.READY:
                raise RuntimeError(view.message or f"{project.display_name} 未能启动。")

        projects_by_id = {project.id: project for project in projects}

        def endpoint_info(project_id: str) -> tuple[int, str] | None:
            unit = manager.unit_for(project_id)
            if unit is None or unit.codex.state != EngineState.READY:
                return None
            return unit.project.codexpro_port, unit.project.root_path

        device = DeviceRegistry(
            local_device_id=request["device_id"],
            local_device_name=request["device_name"],
        )
        port = ports[0]
        gateway = OAuthGateway(
            public_hostname=f"http://127.0.0.1:{port}",
            upstream_legacy_token=lambda: token,
            workspace_registry=endpoint_info,
            workspace_project_registry=lambda project_id: (
                projects_by_id[project_id].root_path if project_id in projects_by_id else None
            ),
            workspace_credential_registry=lambda project_id: (
                token if project_id in projects_by_id else None
            ),
            device_registry=device,
            local_device_id=request["device_id"],
            allow_local_anonymous=False,
        )
        gateway.start(port)
        _send_startup({"port": port})
        closed.wait()
    except (ValueError, TypeError, KeyError, RuntimeError, OSError) as error:
        with suppress(OSError):
            _send_startup({"error": str(error)[:300]})
        return 1
    finally:
        if gateway is not None:
            with suppress(Exception):
                gateway.stop()
        with suppress(Exception):
            manager.stop_all()
        startup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
