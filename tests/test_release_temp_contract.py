"""Build scripts keep task-owned smoke output and configuration in the repository."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_ci_installs_the_validated_lockfile() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert workflow.count("uv sync --locked --extra dev --extra package") == 2
    assert "uv pip install" not in workflow


def test_linux_build_uses_owned_temp_and_app_configuration() -> None:
    script = (ROOT / "scripts/build_linux.sh").read_text(encoding="utf-8")
    assert '/tmp/mcp-devbridge-linux-smoke.log' not in script
    assert 'mktemp -d "$ROOT/.build-temp-' in script
    assert 'export LOCALDEV_MCP_CONFIG_DIR="$BUILD_TEMP/app-config"' in script
    assert 'SMOKE_LOG="$BUILD_TEMP/frozen-smoke.log"' in script
    assert script.index("scripts/check_release_version.py") < script.index("mktemp -d")
    assert "trap 'rm -rf -- \"$BUILD_TEMP\"' EXIT" in script
