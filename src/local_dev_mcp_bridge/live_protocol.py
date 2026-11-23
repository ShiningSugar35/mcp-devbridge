"""Protocol limits and virtual paths shared by the MCP Live relay and client."""

from __future__ import annotations

MAX_FOLDERS = 32
POLL_SECONDS = 8
TEXT_LIMIT = 32_768


def folder_alias(value: str) -> str:
    if (
        not 1 <= len(value) <= 80
        or value.strip() != value
        or value in {".", "..", "~"}
        or not value.isprintable()
        or any(character in value for character in "/\\:")
    ):
        raise ValueError("文件夹名称须为 1 至 80 个可显示字符，不可含路径分隔符。")
    return value


def folder_path(path: str) -> tuple[str, str]:
    alias, _, relative = path.replace("\\", "/").partition("/")
    if not alias or not relative:
        raise ValueError("多个目录在线时，请使用「文件夹名称/相对路径」。")
    return alias.casefold(), relative
