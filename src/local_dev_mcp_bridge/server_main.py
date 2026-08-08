"""Backend CLI entrypoint: run the MCP Server over Streamable HTTP on 127.0.0.1."""

from __future__ import annotations

import argparse
import socket
import sys

import uvicorn

from . import constants
from .config_store import load_runtime_config
from .secrets import SecretsStore, generate_token
from .server_factory import build_backend


def ensure_access_token() -> str:
    """Ensure a public-access bearer token exists in the secure store."""
    store = SecretsStore()
    existing = store.get(constants.ACCESS_TOKEN_CRED_NAME)
    if existing:
        return existing
    token = generate_token(256)
    store.set(constants.ACCESS_TOKEN_CRED_NAME, token)
    return token


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localdev-mcp-bridge-backend")
    parser.add_argument("--config", default=str(constants.RC_FILE), help="runtime configuration JSON path")
    parser.add_argument("--port", type=int, default=0, help="override local port")
    args = parser.parse_args(argv)

    rc = load_runtime_config(args.config)
    if rc is None:
        print(f"[backend] 无法读取运行时配置: {args.config}", flush=True)
        return 2

    if args.port:
        rc.legacy_backend_port = args.port

    try:
        ensure_access_token()
    except Exception as exc:
        print(f"[backend] 访问密钥初始化失败: {exc}", flush=True)
        return 3

    if port_in_use(rc.legacy_backend_port):
        print(
            f"[backend] 端口 {rc.legacy_backend_port} 已被占用。若为旧后端实例，请先停止服务；"
            "否则请更换本地端口（legacy_backend_port）。",
            flush=True,
        )
        return 4

    mcp_server, app, tools = build_backend(rc)
    print(
        f"[backend] 启动: workspace={rc.workspace!r} mode={rc.permission_mode} "
        f"port={rc.legacy_backend_port}",
        flush=True,
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=rc.legacy_backend_port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        print("[backend] 收到中断，退出。", flush=True)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[backend] 启动失败: {type(exc).__name__}: {exc}", flush=True)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())