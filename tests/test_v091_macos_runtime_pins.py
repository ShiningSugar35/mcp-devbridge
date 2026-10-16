"""Reproducible publisher hashes avoid shared-runner API rate exhaustion."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("build_macos_pins", ROOT / "scripts/build_macos.py")
assert _SPEC is not None and _SPEC.loader is not None
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


@pytest.mark.parametrize("arch,digest", [
    ("arm64", "90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09"),
    ("amd64", "70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b"),
])
def test_pins_match_publisher_release_evidence(arch, digest):
    url, actual = build.cloudflared_archive(arch)
    assert actual == digest
    assert url == f"https://github.com/cloudflare/cloudflared/releases/download/2026.7.3/cloudflared-darwin-{arch}.tgz"


def test_unknown_architecture_has_no_fallback():
    with pytest.raises(ValueError):
        build.cloudflared_archive("../arm64")


def test_version_change_requires_new_publisher_evidence(monkeypatch):
    monkeypatch.setattr(build, "CLOUDFLARED_VERSION", "2099.1.1")
    with pytest.raises(ValueError):
        build.cloudflared_archive("arm64")
