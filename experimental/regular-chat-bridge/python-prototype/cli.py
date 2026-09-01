"""Command-line entrypoint for optional MCP DevBridge utilities."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .engines import find_node
from .regular_chat import (
    RegularChatClient,
    RegularChatError,
    install_managed_browser,
    managed_browser_ready,
    regular_chat_runtime_dir,
    reset_profile,
    resolve_regular_chat_paths,
    workspace_hash,
)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _chat_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    chat = subparsers.add_parser("chat", help="Regular Chat 独立浏览器控制")
    chat.add_argument("--workspace", default=".", help="工作区路径，默认当前目录")
    chat.add_argument(
        "--engine",
        choices=["managed-chromium", "msedge", "chrome"],
        default="managed-chromium",
        help="独立浏览器引擎",
    )
    commands = chat.add_subparsers(dest="chat_command", required=True)
    commands.add_parser("login", help="打开独立登录浏览器")
    commands.add_parser("status", help="显示 Controller 状态")
    send = commands.add_parser("send", help="发送一轮消息")
    send.add_argument("--run-id", required=True)
    send.add_argument("--local-turn-id", default="", help="可选；重连重试时复用同一 turn id 以抑制重复发送")
    send.add_argument("--prompt", required=True)
    send.add_argument("--intent", choices=["read_only", "mutation"], default="read_only")
    send.add_argument("--conversation-url", default="")
    watch = commands.add_parser("watch", help="等待当前轮完成")
    watch.add_argument("--run-id", required=True)
    watch.add_argument("--timeout-ms", type=int, default=120000)
    continuation = commands.add_parser("continue", help="仅在上一轮安全完成后发送下一轮")
    continuation.add_argument("--run-id", required=True)
    continuation.add_argument("--local-turn-id", required=True)
    continuation.add_argument("--prompt", required=True)
    continuation.add_argument("--intent", choices=["read_only", "mutation"], default="read_only")
    resume = commands.add_parser("resume", help="恢复同一 durable run 会话")
    resume.add_argument("--run-id", required=True)
    profile = commands.add_parser("profile", help="独立登录环境管理")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_sub.add_parser("list")
    reset = profile_sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true", help="确认删除独立登录环境")
    commands.add_parser("doctor", help="诊断 Regular Chat 运行环境")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcpdev")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _chat_parser(subparsers)
    return parser


def _client(args: argparse.Namespace) -> RegularChatClient:
    return RegularChatClient(engine=args.engine, headed=True)


def _chat(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    whash = workspace_hash(workspace)
    if args.chat_command == "doctor":
        paths = resolve_regular_chat_paths()
        _json(
            {
                "controller_runtime": paths.controller_entry.is_file(),
                "node_runtime": bool(find_node()),
                "managed_browser": managed_browser_ready(),
                "runtime_root": str(paths.runtime_root),
                "controller_entry": str(paths.controller_entry),
            }
        )
        return 0
    if args.chat_command == "profile":
        if args.profile_command == "list":
            root = regular_chat_runtime_dir() / "profiles"
            _json({"profiles": sorted(item.name for item in root.iterdir()) if root.is_dir() else []})
            return 0
        if not args.yes:
            print("拒绝删除：请显式传入 --yes。", file=sys.stderr)
            return 2
        target = reset_profile(args.engine)
        _json({"ok": True, "removed_profile": str(target)})
        return 0

    if args.engine == "managed-chromium" and not managed_browser_ready():
        if args.chat_command == "login":
            print("正在安装与当前 Playwright 版本匹配的独立浏览器…", file=sys.stderr)
            install_managed_browser()
        else:
            raise RegularChatError("独立浏览器尚未安装，请先运行 `mcpdev chat login`。")

    client = _client(args)
    try:
        if args.chat_command == "login":
            result = client.request("profile.login")
            _json(result)
            if sys.stdin.isatty():
                input("请在打开的独立浏览器中完成登录；完成后按回车关闭浏览器。")
            return 0
        if args.chat_command == "status":
            _json(client.request("controller.status"))
            return 0
        if args.chat_command in {"send", "watch", "continue", "resume"}:
            run_id = args.run_id
            open_params: dict[str, Any] = {"workspace_hash": whash, "run_id": run_id}
            conversation_url = getattr(args, "conversation_url", "")
            if conversation_url:
                open_params["conversation_url"] = conversation_url
            client.request("session.resume" if args.chat_command != "send" else "session.open", open_params)
            if args.chat_command == "resume":
                _json(client.request("controller.status"))
                return 0
            if args.chat_command == "send":
                _json(
                    client.request(
                        "turn.send",
                        {
                            "run_id": run_id,
                            "prompt": args.prompt,
                            "intent_class": args.intent,
                            **({"local_turn_id": args.local_turn_id} if args.local_turn_id else {}),
                        },
                    )
                )
                return 0
            if args.chat_command == "continue":
                _json(
                    client.request(
                        "turn.continue",
                        {
                            "run_id": run_id,
                            "local_turn_id": args.local_turn_id,
                            "prompt": args.prompt,
                            "intent_class": args.intent,
                        },
                    )
                )
                return 0
            _json(
                client.request(
                    "turn.watch",
                    {"run_id": run_id, "timeout_ms": args.timeout_ms},
                    timeout_seconds=min(900.0, max(30.0, args.timeout_ms / 1000.0 + 15.0)),
                )
            )
            return 0
        raise RegularChatError(f"未知 chat 命令：{args.chat_command}")
    finally:
        client.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "chat":
            return _chat(args)
        return 2
    except (RegularChatError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Regular Chat 操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
