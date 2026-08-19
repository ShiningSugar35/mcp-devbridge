from pathlib import Path


def test_installer_removes_stale_codexpro_runtime_before_upgrade() -> None:
    text = Path("scripts/installer.iss").read_text(encoding="utf-8")
    assert "[InstallDelete]" in text
    assert 'Type: filesandordirs; Name: "{app}\\_internal\\third_party\\codexpro"' in text


def test_pyinstaller_uses_generated_production_codexpro_runtime() -> None:
    text = Path("packaging/local-dev-mcp-bridge.spec").read_text(encoding="utf-8")
    assert 'CODEXPRO_RUNTIME = ROOT / "build" / "codexpro-runtime"' in text
    assert 'ROOT / "third_party" / "codexpro" / "node_modules"' not in text

def test_linux_build_prepares_production_codexpro_runtime() -> None:
    text = Path("scripts/build_linux.sh").read_text(encoding="utf-8")
    assert '"$PY" scripts/prepare_codexpro_runtime.py' in text

