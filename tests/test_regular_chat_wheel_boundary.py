from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PACKAGE = ROOT / "src" / "local_dev_mcp_bridge"
ARCHIVED_PROTOTYPE = ROOT / "experimental" / "regular-chat-bridge" / "python-prototype"


def test_regular_chat_python_prototypes_are_outside_the_production_wheel_package() -> None:
    production_only_names = ("regular_chat.py", "regular_chat_ui.py", "cli.py")
    leaked = [name for name in production_only_names if (PRODUCTION_PACKAGE / name).exists()]
    assert leaked == [], f"experimental Regular Chat modules leaked into production package: {leaked}"

    for name in (
        "README.md",
        "regular_chat.py",
        "regular_chat_ui.py",
        "cli.py",
        "entry_cli.py",
        "prepare_regular_chat_runtime.py",
        "test_regular_chat.py",
    ):
        assert (ARCHIVED_PROTOTYPE / name).is_file(), f"missing archived experimental asset: {name}"
