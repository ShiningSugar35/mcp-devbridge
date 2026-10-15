"""Mac package contents and archive extraction contracts without native tools."""
from __future__ import annotations

import ast
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("build_macos", ROOT / "scripts" / "build_macos.py")
assert _SPEC is not None and _SPEC.loader is not None
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


def test_mac_bundle_preserves_node_package_metadata():
    source = (ROOT / "packaging" / "macos.spec").read_text(encoding="utf-8")
    tree = ast.parse(source)
    analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Analysis")
    data = next(item.value for item in analysis.keywords if item.arg == "datas")
    assert any(isinstance(node, ast.Constant) and node.value == "package.json" for node in ast.walk(data))


def _archive(path: Path, members: list[tuple[str, bytes, bool]]) -> None:
    with tarfile.open(path, "w:gz") as output:
        for name, value, symlink in members:
            info = tarfile.TarInfo(name)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "somewhere-else"
                output.addfile(info)
            else:
                info.size = len(value)
                output.addfile(info, io.BytesIO(value))


def test_runtime_extraction_copies_only_requested_regular_file(tmp_path):
    archive = tmp_path / "runtime.tgz"
    _archive(archive, [("unrelated/note", b"ignore", False), ("cloudflared", b"native-placeholder", False)])
    destination = tmp_path / "output"
    build.extract_runtime(archive, "cloudflared", destination)
    assert destination.read_bytes() == b"native-placeholder"
    assert not (tmp_path / "unrelated").exists()


@pytest.mark.parametrize("members", [
    [("cloudflared", b"", True)],
    [("cloudflared", b"a", False), ("cloudflared", b"b", False)],
    [("wrong-member", b"a", False)],
])
def test_runtime_extraction_requires_unique_regular_member(tmp_path, members):
    archive = tmp_path / "runtime.tgz"
    _archive(archive, members)
    destination = tmp_path / "output"
    with pytest.raises(RuntimeError):
        build.extract_runtime(archive, "cloudflared", destination)
    assert not destination.exists()
