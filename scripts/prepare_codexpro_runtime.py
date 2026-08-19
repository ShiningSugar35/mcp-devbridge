from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party" / "codexpro"
TARGET = ROOT / "build" / "codexpro-runtime"


def _copy_package(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("node_modules", ".bin"),
    )


def main() -> int:
    lock_path = SOURCE / "package-lock.json"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = payload.get("packages")
    if payload.get("lockfileVersion") != 3 or not isinstance(packages, dict):
        raise RuntimeError("CodexPro package-lock.json must use lockfileVersion 3.")

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    for name in ("package.json", "package-lock.json"):
        shutil.copy2(SOURCE / name, TARGET / name)
    shutil.copytree(SOURCE / "dist", TARGET / "dist")

    copied = 0
    skipped_dev = 0
    for relative, metadata in sorted(packages.items(), key=lambda item: item[0].count("/")):
        if not relative.startswith("node_modules/"):
            continue
        if not isinstance(metadata, dict):
            continue
        if bool(metadata.get("dev")):
            skipped_dev += 1
            continue
        source = SOURCE / Path(relative)
        if not source.is_dir():
            raise RuntimeError(f"Production dependency missing from source tree: {relative}")
        target = TARGET / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_package(source, target)
        copied += 1

    total_bytes = sum(path.stat().st_size for path in TARGET.rglob("*") if path.is_file())
    print(f"CodexPro production runtime: {copied} packages, {skipped_dev} dev packages skipped")
    print(f"CodexPro production runtime size: {total_bytes / 1024 / 1024:.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
