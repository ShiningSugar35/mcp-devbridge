from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "scripts" / "check_release_version.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("check_release_version_under_test", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fixture(root: Path, *, versions: dict[str, str]) -> None:
    (root / "src" / "local_dev_mcp_bridge").mkdir(parents=True)
    (root / "packaging").mkdir()
    (root / "scripts").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "local-dev-mcp-bridge"\n'
        f'version = "{versions["pyproject"]}"\n',
        encoding="utf-8",
    )
    (root / "src" / "local_dev_mcp_bridge" / "__init__.py").write_text(
        f'__version__ = "{versions["package"]}"\n',
        encoding="utf-8",
    )
    (root / "packaging" / "local-dev-mcp-bridge.spec").write_text(
        f'PROJECT_VERSION = "{versions["spec"]}"\n',
        encoding="utf-8",
    )
    (root / "scripts" / "installer.iss").write_text(
        f'#define MyAppVersion "{versions["installer"]}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "local-dev-mcp-bridge"\n'
        f'version = "{versions["lock"]}"\n',
        encoding="utf-8",
    )


def test_current_repository_release_versions_match() -> None:
    helper = _load_helper()
    result = helper.check_release_versions(REPO_ROOT, "0.8.9")
    assert result.ok
    assert result.mismatches == ()
    assert set(result.observed) == {
        "pyproject.toml",
        "package __version__",
        "PyInstaller PROJECT_VERSION",
        "Inno MyAppVersion",
        "uv lock project version",
    }


def test_version_gate_reports_all_mismatches(tmp_path: Path) -> None:
    helper = _load_helper()
    _write_fixture(
        tmp_path,
        versions={
            "pyproject": "0.8.8",
            "package": "0.8.7",
            "spec": "0.8.6",
            "installer": "0.8.5",
            "lock": "0.8.4",
        },
    )

    result = helper.check_release_versions(tmp_path, "0.8.9")

    assert not result.ok
    assert len(result.mismatches) == 5
    assert all("expected 0.8.9" in item for item in result.mismatches)
    assert result.observed["pyproject.toml"] == "0.8.8"
    assert result.observed["uv lock project version"] == "0.8.4"


def test_cli_is_fail_closed_before_build_side_effects(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path,
        versions={key: "0.8.8" for key in ("pyproject", "package", "spec", "installer", "lock")},
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(HELPER_PATH),
            "--root",
            str(tmp_path),
            "--expected",
            "0.8.9",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
    assert "release version mismatch" in completed.stderr
    assert "pyproject.toml" in completed.stderr
    assert "uv lock project version" in completed.stderr


def test_linux_build_invokes_gate_before_runtime_or_dependency_work() -> None:
    script = (REPO_ROOT / "scripts" / "build_linux.sh").read_text(encoding="utf-8")
    gate_index = script.index("check_release_version.py")
    assert gate_index < script.index("prepare_runtime_linux.sh")
    assert gate_index < script.index("npm ci")
    assert gate_index < script.index('PY="$ROOT/.venv/bin/python"')


def test_helper_rejects_missing_or_malformed_version_sources(tmp_path: Path) -> None:
    helper = _load_helper()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    result = helper.check_release_versions(tmp_path, "0.8.9")

    assert not result.ok
    assert len(result.mismatches) == 5
    assert any("missing or malformed" in item for item in result.mismatches)


@pytest.mark.parametrize("value", ["", "0.8", "v0.8.9", "0.8.9/../../x"])
def test_cli_rejects_invalid_expected_version(value: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--expected", value],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
