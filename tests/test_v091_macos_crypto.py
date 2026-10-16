"""Prevent the Intel frozen OpenSSL ABI collision without modifying runtime TLS."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _checker():
    spec = importlib.util.spec_from_file_location("check_macos_crypto", ROOT / "scripts/check_macos_crypto.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_static_crypto_accepts_system_dependencies():
    text = "/tmp/_rust.abi3.so:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n"
    assert _checker().openssl_dependencies(text) == []


@pytest.mark.parametrize("library", [
    "/usr/local/opt/openssl@3/lib/libssl.3.dylib",
    "@rpath/libcrypto.3.dylib",
    "@loader_path/libssl.1.1.dylib",
    "/Library/Application Support/openssl/libcrypto.dylib",
])
def test_dynamic_crypto_dependencies_are_detected(library):
    text = f"/tmp/_rust.abi3.so:\n\t{library} (compatibility version 3.0.0, current version 3.6.0)\n"
    assert _checker().openssl_dependencies(text) == [library]


def test_missing_otool_dependency_table_is_not_reported_as_static():
    with pytest.raises(ValueError):
        _checker().openssl_dependencies("unexpected output")


def test_intel_workflow_builds_static_crypto_without_reusing_dynamic_wheel():
    workflow = (ROOT / ".github/workflows/macos.yml").read_text(encoding="utf-8")
    assert "OPENSSL_STATIC=1" in workflow
    assert "--no-binary-package cryptography" in workflow
    assert "--reinstall-package cryptography" in workflow
    assert "--no-cache" in workflow
    assert "brew upgrade openssl@3 || true" not in workflow


def test_native_build_checks_crypto_before_freezing():
    text = (ROOT / "scripts/build_macos.py").read_text(encoding="utf-8")
    assert text.index('"scripts/check_macos_crypto.py"') < text.index('"-m", "PyInstaller"')
