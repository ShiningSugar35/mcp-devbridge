"""Gemini 静态 OAuth Client 管理 CLI（开发/运维辅助）。

无需打开 GUI 即可为指定 redirect URI 创建（或复用）预注册的
confidential client，并把 client_id / client_secret 安全存入
SecretsStore（Windows 凭据管理器 / DPAPI 回退文件）。

示例：
    python -m local_dev_mcp_bridge.gemini_oauth --redirect-uri https://g.geminiapp.com/...
    python -m local_dev_mcp_bridge.gemini_oauth --redirect-uri <URI> --rotate   # 重生成 secret（旧 secret 立即失效）
    python -m local_dev_mcp_bridge.gemini_oauth --redirect-uri <URI> --print-secret  # 显式打印 secret（默认不打印）
"""

from __future__ import annotations

import argparse
import sys

from .oauth_provider import get_or_create_gemini_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gemini_oauth",
        description="创建/复用 Gemini Spark 静态 OAuth client（client_secret 加密存储，默认不打印）。",
    )
    parser.add_argument(
        "--redirect-uri", required=True, help="Gemini Custom Connected App 里的 redirect URI（Copy redirect URI）。"
    )
    parser.add_argument("--rotate", action="store_true", help="重新生成 client_secret（旧值立即失效）。")
    parser.add_argument(
        "--print-secret", action="store_true", help="把 client_secret 打印到终端（默认脱敏，仅显示提示）。"
    )
    args = parser.parse_args(argv)

    try:
        client_id, secret = get_or_create_gemini_client(args.redirect_uri, rotate_secret=args.rotate)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    action = "重新生成（旧 secret 已失效）" if args.rotate else "复用已有 client"
    print(f"[{action}]")
    print(f"Client ID:   {client_id}")
    print("Client Secret: " + (secret if args.print_secret else "（未打印；如确需可加 --print-secret 查看，注意终端勿入日志）"))
    print("填写位置：Gemini → Settings → Custom Connected App → Advanced Settings。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())