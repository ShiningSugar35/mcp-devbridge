"""Four-segment release gate and update ordering; only local fixtures."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import local_dev_mcp_bridge.update_manager as updates


def test_four_segment_release_gate(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": '[project]\nversion = "0.8.9.1"\n',
        "src/local_dev_mcp_bridge/__init__.py": '__version__ = "0.8.9.1"\n',
        "packaging/local-dev-mcp-bridge.spec": 'PROJECT_VERSION = "0.8.9.1"\n',
        "scripts/installer.iss": '#define MyAppVersion "0.8.9.1"\n',
        "uv.lock": '[[package]]\nname = "local-dev-mcp-bridge"\nversion = "0.8.9.1"\n',
    }
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    helper = Path(__file__).resolve().parents[1] / "scripts/check_release_version.py"
    completed = subprocess.run(
        [sys.executable, str(helper), "--root", str(tmp_path), "--expected", "0.8.9.1"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("earlier", ["0.8.9", "0.8.9-fixed", "0.8.9.post1", "0.8.9.post99"])
def test_fourth_release_segment_is_not_a_post_release(earlier: str) -> None:
    assert updates.is_newer("0.8.9.1", earlier)
    assert not updates.is_newer(earlier, "0.8.9.1")
    assert not updates.is_newer("0.8.9.1", "0.8.9.1")
    assert updates.is_newer("0.8.9.2", "0.8.9.1")
    assert updates.is_newer("0.8.10", "0.8.9.99")


def test_four_segment_asset_is_discoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "IS_WINDOWS", True)
    monkeypatch.setattr(updates, "IS_LINUX", False)
    payload = []
    for version in ("0.8.9-fixed", "0.8.9.1", "0.8.9"):
        payload.append(
            {
                "tag_name": "v" + version,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": f"MCPDevBridge-Setup-{version}.exe",
                        "browser_download_url": "https://example.invalid/fixture.exe",
                        "size": 123,
                    }
                ],
            }
        )
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", updates.RELEASES_API))
    monkeypatch.setattr(updates.httpx, "get", lambda *args, **kwargs: response)
    latest = updates.fetch_latest_release()
    assert latest.version == "0.8.9.1"
    assert latest.asset_name == "MCPDevBridge-Setup-0.8.9.1.exe"


@pytest.mark.parametrize("version", ["0.8.9.1.2", "0.8.9.1-rc1", "0.8.9.1/../../x", "v"])
def test_unsupported_update_labels_stay_ineligible(version: str) -> None:
    assert updates.version_tuple(version) == (0,)
