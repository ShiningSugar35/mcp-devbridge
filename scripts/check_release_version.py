from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class VersionCheckResult:
    expected: str
    observed: dict[str, str]
    mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _pyproject_version(root: Path) -> str:
    text = _read_text(root / "pyproject.toml")
    section = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if not section:
        return ""
    match = re.search(
        r'^\s*version\s*=\s*["\']([^"\']+)["\']\s*$',
        section.group(1),
        re.MULTILINE,
    )
    return str(match.group(1) if match else "").strip()


def _regex_version(root: Path, relative: str, pattern: str) -> str:
    match = re.search(pattern, _read_text(root / relative), flags=re.MULTILINE)
    return str(match.group(1) if match else "").strip()


def _package_version(root: Path) -> str:
    return _regex_version(
        root,
        "src/local_dev_mcp_bridge/__init__.py",
        r'^\s*__version__\s*=\s*["\']([^"\']+)["\']\s*$',
    )


def _spec_version(root: Path) -> str:
    return _regex_version(
        root,
        "packaging/local-dev-mcp-bridge.spec",
        r'^\s*PROJECT_VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
    )


def _installer_version(root: Path) -> str:
    return _regex_version(
        root,
        "scripts/installer.iss",
        r'^\s*#define\s+MyAppVersion\s+["\']([^"\']+)["\']\s*$',
    )


def _lock_version(root: Path) -> str:
    text = _read_text(root / "uv.lock")
    for block in re.split(r"(?m)^\[\[package\]\]\s*$", text)[1:]:
        name = re.search(
            r'^\s*name\s*=\s*["\']([^"\']+)["\']\s*$',
            block,
            re.MULTILINE,
        )
        if not name or name.group(1) != "local-dev-mcp-bridge":
            continue
        version = re.search(
            r'^\s*version\s*=\s*["\']([^"\']+)["\']\s*$',
            block,
            re.MULTILINE,
        )
        return str(version.group(1) if version else "").strip()
    return ""


def check_release_versions(root: Path, expected: str) -> VersionCheckResult:
    root = root.expanduser().resolve()
    readers: tuple[tuple[str, Callable[[Path], str]], ...] = (
        ("pyproject.toml", _pyproject_version),
        ("package __version__", _package_version),
        ("PyInstaller PROJECT_VERSION", _spec_version),
        ("Inno MyAppVersion", _installer_version),
        ("uv lock project version", _lock_version),
    )
    observed: dict[str, str] = {}
    mismatches: list[str] = []
    for label, reader in readers:
        try:
            value = reader(root)
        except (OSError, ValueError):
            value = ""
        observed[label] = value
        if not value:
            mismatches.append(f"{label}: missing or malformed (expected {expected})")
        elif value != expected:
            mismatches.append(f"{label}: found {value}, expected {expected}")
    return VersionCheckResult(
        expected=expected,
        observed=observed,
        mismatches=tuple(mismatches),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail closed unless every MCP DevBridge release-version source matches."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the parent of scripts/).",
    )
    parser.add_argument("--expected", required=True, help="Expected release version.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected = str(args.expected or "").strip()
    if not _VERSION_PATTERN.fullmatch(expected):
        print(
            f"invalid expected release version: {expected!r}; use X.Y.Z or SemVer suffixes",
            file=sys.stderr,
        )
        return 2

    result = check_release_versions(args.root, expected)
    if not result.ok:
        print("release version mismatch:", file=sys.stderr)
        for mismatch in result.mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 2

    print(f"release versions match: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
