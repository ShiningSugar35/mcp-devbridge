from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_regular_chat_controller_is_not_wired_into_formal_product_surfaces() -> None:
    desktop = _text("src/local_dev_mcp_bridge/desktop_main.py")
    pyproject = _text("pyproject.toml")
    windows_build = _text("scripts/build.ps1")
    linux_build = _text("scripts/build_linux.sh")
    spec = _text("packaging/local-dev-mcp-bridge.spec")

    assert "RegularChatWidget" not in desktop
    assert "regular_chat_tab" not in desktop
    assert "mcpdev =" not in pyproject
    assert "regular-chat-controller" not in windows_build
    assert "regular-chat-controller" not in linux_build
    assert "regular-chat-controller" not in spec
    assert "local_dev_mcp_bridge.regular_chat" not in spec
    assert "local_dev_mcp_bridge.cli" not in spec
    assert not (ROOT / "packaging" / "entry_cli.py").exists()
    assert not (ROOT / "scripts" / "prepare_regular_chat_runtime.py").exists()
    assert not (ROOT / "src" / "local_dev_mcp_bridge" / "cli.py").exists()
    assert not (ROOT / "src" / "local_dev_mcp_bridge" / "regular_chat.py").exists()
    assert not (ROOT / "src" / "local_dev_mcp_bridge" / "regular_chat_ui.py").exists()


def test_experimental_controller_source_and_policy_notice_are_retained() -> None:
    controller = ROOT / "third_party" / "regular-chat-controller"
    assert (controller / "src" / "controller.ts").is_file()
    assert (controller / "fixture-tests" / "controllerRecovery.test.ts").is_file()
    readme = (controller / "README.md").read_text(encoding="utf-8")
    assert "unreleased experimental implementation" in readme
    assert "must not be used" in readme

    python_prototype = ROOT / "experimental" / "regular-chat-bridge" / "python-prototype"
    for name in (
        "README.md",
        "regular_chat.py",
        "regular_chat_ui.py",
        "cli.py",
        "entry_cli.py",
        "prepare_regular_chat_runtime.py",
        "test_regular_chat.py",
    ):
        assert (python_prototype / name).is_file()
