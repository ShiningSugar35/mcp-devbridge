"""File system tool tests: paths, encodings, writes, patches, deletes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from local_dev_mcp_bridge.permissions import PermissionError as PermissionDenied
from local_dev_mcp_bridge.tools import (
    LocalDevTools,
    apply_unified_patch,
    atomically_write,
    parse_unified_diff,
)


class TestPaths:
    def test_chinese_path_listing(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "中文目录").mkdir()
        (workspace / "中文目录" / "文件.txt").write_text("内容", encoding="utf-8")
        listing = tools.list_directory("")
        assert "中文目录/" in listing

    def test_space_path_listing(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "my folder").mkdir()
        listing = tools.list_directory("")
        assert "my folder/" in listing

    def test_relative_and_absolute(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "a.txt").write_text("hello", encoding="utf-8")
        assert "hello" in tools.read_file("a.txt")
        assert "hello" in tools.read_file(str(workspace / "a.txt"))

    def test_path_traversal_blocked(self, tools: LocalDevTools, tmp_path: Path) -> None:
        with pytest.raises(PermissionDenied):
            tools.read_file("../outside.txt")
        with pytest.raises(PermissionDenied):
            tools.list_directory(str(tmp_path))

    def test_excluded_dir_blocked(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / ".venv").mkdir()
        with pytest.raises(PermissionDenied):
            tools.list_directory(".venv")

    def test_system_mode_allows_external(self, system_tools: LocalDevTools, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("external", encoding="utf-8")
        assert "external" in system_tools.read_file(str(outside))


class TestReading:
    def test_utf8_bom(self, tools: LocalDevTools, workspace: Path) -> None:
        path = workspace / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbf" + "你好".encode())
        assert "你好" in tools.read_file("bom.txt")

    def test_gb18030(self, tools: LocalDevTools, workspace: Path) -> None:
        path = workspace / "gb.txt"
        path.write_bytes("中文内容".encode("gb18030"))
        assert "中文内容" in tools.read_file("gb.txt")

    def test_large_file_rejected(self, tools: LocalDevTools, workspace: Path) -> None:
        path = workspace / "big.txt"
        path.write_bytes(b"x" * (tools.max_file_bytes + 1))
        with pytest.raises(ValueError, match="上限"):
            tools.read_file("big.txt")

    def test_read_with_line_numbers(self, tools: LocalDevTools, workspace: Path) -> None:
        path = workspace / "lines.txt"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        text = tools.read_file("lines.txt")
        assert "1: a" in text and "3: c" in text

    def test_read_files_multi_and_limit(self, tools: LocalDevTools, workspace: Path) -> None:
        for i in range(3):
            (workspace / f"f{i}.txt").write_text("x" * 100, encoding="utf-8")
        out = tools.read_files(["f0.txt", "f1.txt", "f2.txt"])
        assert "f0.txt" in out
        with pytest.raises(ValueError, match="最多"):
            tools.read_files([f"f{i}.txt" for i in range(30)])

    def test_search_text(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "s.txt").write_text("hello 世界\nfoo\nhello again\n", encoding="utf-8")
        hits = tools.search_text("hello")
        assert hits.count("hello") == 2

    def test_search_regex(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "r.txt").write_text("abc\nxyz\n", encoding="utf-8")
        hits = tools.search_text(r"a.c", regex=True)
        assert "r.txt:1" in hits

    def test_find_files(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "main.py").write_text("", encoding="utf-8")
        (workspace / "main.pyc").write_text("", encoding="utf-8")
        (workspace / "note.md").write_text("", encoding="utf-8")
        found = tools.find_files(extension="py")
        assert "main.py" in found and "main.pyc" not in found
        found2 = tools.find_files(name_contains="main")
        assert "main.py" in found2


class TestWriting:
    def test_write_and_atomic(self, tools: LocalDevTools, workspace: Path) -> None:
        tools.write_file("新建.txt", "内容", overwrite=True)
        target = workspace / "新建.txt"
        assert target.read_text(encoding="utf-8") == "内容"
        assert not list(workspace.glob("*.ldmb-tmp"))

    def test_write_requires_overwrite(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "e.txt").write_text("old", encoding="utf-8")
        with pytest.raises(ValueError, match="已存在"):
            tools.write_file("e.txt", "new")
        assert (workspace / "e.txt").read_text(encoding="utf-8") == "old"

    def test_write_sha256_guard(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "g.txt").write_text("v1", encoding="utf-8")
        expected = hashlib.sha256(b"v1").hexdigest()
        tools.write_file("g.txt", "v2", overwrite=True, expected_sha256=expected)
        assert (workspace / "g.txt").read_text(encoding="utf-8") == "v2"
        with pytest.raises(ValueError, match="SHA256"):
            tools.write_file("g.txt", "v3", overwrite=True, expected_sha256="deadbeef")

    def test_replace_exact_count(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "r.txt").write_text("a b a", encoding="utf-8")
        with pytest.raises(ValueError, match="实际"):
            tools.replace_text("r.txt", "a", "x", expected_count=3)
        tools.replace_text("r.txt", "a", "x", expected_count=2)
        assert (workspace / "r.txt").read_text(encoding="utf-8") == "x b x"

    def test_atomic_write_helper(self, workspace: Path) -> None:
        atomically_write(workspace / "h.txt", b"data")
        assert (workspace / "h.txt").read_text(encoding="utf-8") == "data"

    def test_make_directory(self, tools: LocalDevTools, workspace: Path) -> None:
        tools.make_directory("deep/nested/目录")
        assert (workspace / "deep/nested/目录").is_dir()

    def test_copy_move(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "c.txt").write_text("copy", encoding="utf-8")
        tools.copy_path("c.txt", "d.txt")
        assert (workspace / "d.txt").is_file()
        tools.move_path("d.txt", "e.txt")
        assert not (workspace / "d.txt").exists()
        assert (workspace / "e.txt").is_file()

    def test_delete_file_and_recursive_dir(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "del.txt").write_text("x", encoding="utf-8")
        tools.delete_path("del.txt")
        assert not (workspace / "del.txt").exists()
        (workspace / "tree" / "sub").mkdir(parents=True)
        (workspace / "tree" / "sub" / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="recursive"):
            tools.delete_path("tree")
        tools.delete_path("tree", recursive=True)
        assert not (workspace / "tree").exists()

    def test_read_only_mode_blocks_writes(self, read_only_tools: LocalDevTools, workspace: Path) -> None:
        with pytest.raises(PermissionDenied):
            read_only_tools.write_file("x.txt", "x")
        with pytest.raises(PermissionDenied):
            read_only_tools.delete_path("x.txt", recursive=True)
        with pytest.raises(PermissionDenied):
            read_only_tools.run_command("echo hi")


class TestDiff:
    def test_apply_patch_success(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "p.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        diff = (
            "--- a/p.txt\n+++ b/p.txt\n"
            "@@ -1,3 +1,3 @@\n line1\n-line2\n+LINE2\n line3\n"
        )
        result = tools.apply_patch(diff)
        assert "已应用补丁" in result
        assert (workspace / "p.txt").read_text(encoding="utf-8") == "line1\nLINE2\nline3\n"

    def test_apply_patch_context_mismatch_rollback(self, tools: LocalDevTools, workspace: Path) -> None:
        (workspace / "q.txt").write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        diff = (
            "--- a/q.txt\n+++ b/q.txt\n"
            "@@ -1,3 +1,3 @@\n aaa\n-zzz\n+ZZZ\n ccc\n"
        )
        with pytest.raises(ValueError, match="不匹配"):
            tools.apply_patch(diff)
        assert (workspace / "q.txt").read_text(encoding="utf-8") == "aaa\nbbb\nccc\n"

    def test_apply_unified_patch_unit(self) -> None:
        content = "1\n2\n3\n4\n"
        diff = "@@ -1,4 +1,4 @@\n 1\n-2\n+X\n 3\n 4\n"
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1
        new_content, count = apply_unified_patch(content, hunks[0]["hunks"])
        assert count == 1
        assert new_content == "1\nX\n3\n4\n"

    def test_apply_patch_multi_hunk(self) -> None:
        content = "a\nb\nc\nd\ne\n"
        diff = (
            "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
            "@@ -3,3 +3,3 @@\n c\n-d\n+D\n e\n"
        )
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1 and len(hunks[0]["hunks"]) == 2
        new_content, count = apply_unified_patch(content, hunks[0]["hunks"])
        assert count == 2
        assert new_content == "a\nB\nc\nD\ne\n"
