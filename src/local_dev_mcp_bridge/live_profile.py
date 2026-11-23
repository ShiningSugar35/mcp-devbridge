"""Project list and non-secret settings for the portable GUI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


def _profile_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "MCP-Live-settings.json"
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".config") / "MCP Live" / "profile.json"


def load_profile() -> dict:
    try:
        data = json.loads(_profile_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(data: dict) -> None:
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".profile-", delete=False
    ) as temp:
        temp_path = Path(temp.name)
        try:
            json.dump(data, temp, ensure_ascii=False, indent=2)
        except BaseException:
            temp.close()
            temp_path.unlink(missing_ok=True)
            raise
    try:
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
