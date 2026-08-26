from __future__ import annotations

import httpx

import local_dev_mcp_bridge.update_manager as updates


def _release(tag: str, *, prerelease: bool = False, windows_asset: bool = True) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    if windows_asset:
        assets.append(
            {
                "name": f"MCPDevBridge-Setup-{tag.lstrip('v')}.exe",
                "browser_download_url": f"https://example.invalid/{tag}.exe",
                "size": 123,
                "digest": "sha256:" + "a" * 64,
            }
        )
    return {
        "tag_name": tag,
        "name": tag,
        "body": f"notes for {tag}",
        "draft": False,
        "prerelease": prerelease,
        "assets": assets,
    }


def test_fetch_latest_release_jumps_directly_to_highest_stable_platform_build(monkeypatch) -> None:
    monkeypatch.setattr(updates, "IS_WINDOWS", True)
    monkeypatch.setattr(updates, "IS_LINUX", False)
    mismatched_asset = _release("v0.8.9")
    mismatched_assets = mismatched_asset["assets"]
    assert isinstance(mismatched_assets, list) and isinstance(mismatched_assets[0], dict)
    mismatched_assets[0]["name"] = "MCPDevBridge-Setup-0.8.6.exe"
    payload = [
        _release("v0.8.5"),
        _release("v9.9.9-rc1", prerelease=False),  # malformed/non-stable tag must still be ignored
        _release("v0.9.0-rc1", prerelease=True),
        mismatched_asset,
        _release("v0.8.8", windows_asset=False),
        _release("v0.8.7"),
        _release("v0.8.6"),
    ]
    response = httpx.Response(
        200,
        request=httpx.Request("GET", updates.RELEASES_API),
        json=payload,
    )
    monkeypatch.setattr(updates.httpx, "get", lambda *args, **kwargs: response)

    latest = updates.fetch_latest_release()

    assert latest.version == "0.8.7"
    assert latest.tag == "v0.8.7"
    assert latest.asset_name == "MCPDevBridge-Setup-0.8.7.exe"
    assert updates.is_newer(latest.version, "0.8.4")
