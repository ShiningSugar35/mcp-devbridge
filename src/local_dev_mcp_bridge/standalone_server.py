"""Standalone MCP server entrypoint (no GUI): serve one workspace via streamable HTTP."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import constants
from .config_store import save_runtime_config
from .models import RuntimeConfig
from .server_main import main as _backend_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="localdev-mcp-standalone",
        description="以命令行方式启动 MCP DevBridge 后端（无桌面界面）",
    )
    parser.add_argument("workspace", help="项目根目录")
    parser.add_argument("--port", type=int, default=constants.DEFAULT_LEGACY_BACKEND_PORT)
    parser.add_argument("--mode", choices=["read_only", "workspace", "system"], default="workspace")
    parser.add_argument(
        "--execution-profile",
        choices=[p for p in ("safe", "developer", "full_system")],
        default="developer",
        dest="execution_profile",
        help="shell 执行档位（默认 developer：仅开发工具白名单）",
    )
    parser.add_argument(
        "--confirm-full-system",
        action="store_true",
        dest="full_system_confirmed",
        help="一次性确认 full_system 档位的系统级风险（仅该档位需要）",
    )
    parser.add_argument("--auth", choices=["bearer", "anonymous"], default="bearer")
    parser.add_argument(
        "--public-hostname",
        default="",
        help="Named Tunnel 公网域名（例：mcp.example.com），加入 transport_security 白名单",
    )
    parser.add_argument("--public-anonymous", action="store_true", help="允许公网无认证（危险，仅测试）")
    parser.add_argument("--local-anonymous", action="store_true", default=True, help="允许本机无认证访问")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"工作目录不存在: {workspace}", file=sys.stderr)
        return 2

    rc = RuntimeConfig(
        workspace=str(workspace),
        permission_mode=args.mode,
        execution_profile=args.execution_profile,
        full_system_confirmed=args.full_system_confirmed,
        legacy_backend_port=args.port,
        auth_mode=args.auth,
        public_hostname=args.public_hostname.strip(),
        allow_local_anonymous=True,
        require_public_bearer=not args.public_anonymous,
    )
    config_path = constants.RC_FILE
    save_runtime_config(rc, config_path)
    return _backend_main(["--config", str(config_path), "--port", str(args.port)])


if __name__ == "__main__":
    sys.exit(main())