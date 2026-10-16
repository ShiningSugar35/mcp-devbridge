"""Native macOS build. Produces verified architecture-specific .app ZIP candidates.

Requires a native macOS Python/Node toolchain. Never cross-compiles on Windows,
installs a running application, removes quarantine, or claims notarization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "22.19.0"
CLOUDFLARED_VERSION = "2026.7.3"
MAX_DOWNLOAD = 300 * 1024 * 1024
# Publisher release API digests verified 2026-10-16. Keep version and hashes
# together so a dependency upgrade cannot reuse a checksum for another version.
_CLOUDFLARED_ARCHIVES = {
    ("2026.7.3", "amd64"): "70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b",
    ("2026.7.3", "arm64"): "90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09",
}


def cloudflared_archive(architecture: str) -> tuple[str, str]:
    try:
        digest = _CLOUDFLARED_ARCHIVES[(CLOUDFLARED_VERSION, architecture)]
    except KeyError as exc:
        raise ValueError("Cloudflared version/architecture has no verified publisher checksum") from exc
    url = (f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/"
           f"cloudflared-darwin-{architecture}.tgz")
    return url, digest


def small_response(url: str, maximum: int = 2 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MCPDevBridge-native-build"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise RuntimeError("Build metadata exceeds size limit")
    return body


def download_verified(url: str, destination: Path, digest: str) -> None:
    if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
        raise RuntimeError("A SHA-256 from the publisher is required")
    request = urllib.request.Request(url, headers={"User-Agent": "MCPDevBridge-native-build"})
    hasher = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("xb") as target:
        while data := response.read(1024 * 1024):
            total += len(data)
            if total > MAX_DOWNLOAD:
                raise RuntimeError("Runtime download exceeds size limit")
            hasher.update(data)
            target.write(data)
    if hasher.hexdigest() != digest.lower():
        raise RuntimeError("Runtime checksum mismatch")


def extract_runtime(archive: Path, member_name: str, destination: Path) -> None:
    """Copy one regular member to an explicit path; never extract archive paths."""
    matches = []
    with tarfile.open(archive, "r:gz") as source:
        for index, member in enumerate(source):
            if index >= 20000:
                raise RuntimeError("Runtime archive has too many members")
            if member.name.lstrip("./") == member_name:
                matches.append(member)
        if len(matches) != 1 or not matches[0].isfile():
            raise RuntimeError("Expected exactly one regular runtime executable")
        member = matches[0]
        if member.size > 128 * 1024 * 1024:
            raise RuntimeError("Runtime executable exceeds size limit")
        extracted = source.extractfile(member)
        if extracted is None:
            raise RuntimeError("Runtime executable is unreadable")
        with extracted, destination.open("wb") as output:
            shutil.copyfileobj(extracted, output, length=1024 * 1024)
    destination.chmod(0o755)


def prepare_runtimes(temporary: Path, architecture: str) -> Path:
    node_arch = "arm64" if architecture == "arm64" else "x64"
    cf_arch = "arm64" if architecture == "arm64" else "amd64"
    directory = ROOT / ".tools" / ("macos-" + architecture)
    directory.mkdir(parents=True, exist_ok=True)
    node_name = f"node-v{NODE_VERSION}-darwin-{node_arch}.tar.gz"
    node_base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    sums = small_response(node_base + "SHASUMS256.txt").decode("ascii")
    matching = [line.split()[0] for line in sums.splitlines() if line.split()[-1:] == [node_name]]
    if len(matching) != 1:
        raise RuntimeError("Node checksum entry missing or ambiguous")
    node_archive = temporary / node_name
    download_verified(node_base + node_name, node_archive, matching[0])
    extract_runtime(node_archive, f"node-v{NODE_VERSION}-darwin-{node_arch}/bin/node", directory / "node")
    cf_url, cf_digest = cloudflared_archive(cf_arch)
    cf_archive = temporary / f"cloudflared-darwin-{cf_arch}.tgz"
    download_verified(cf_url, cf_archive, cf_digest)
    extract_runtime(cf_archive, "cloudflared", directory / "cloudflared")
    for executable in (directory / "node", directory / "cloudflared"):
        slices = subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], text=True, timeout=10).split()
        if architecture not in slices:
            raise RuntimeError("Downloaded runtime architecture mismatch")
    return directory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() not in {"arm64", "x86_64"}:
        raise RuntimeError("Use a native Apple Silicon or Intel macOS runner")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if args.version != version:
        raise RuntimeError("Requested version differs from project version")
    subprocess.run([sys.executable, "scripts/check_release_version.py", "--root", str(ROOT), "--expected", version], cwd=ROOT, check=True)
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip():
        raise RuntimeError("Native release candidates require a clean tracked source commit")
    architecture = platform.machine()
    parent = ROOT / ".ai-bridge" / "macos-build"
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=architecture + "-", dir=parent))
    env = dict(os.environ)
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = str(temporary)
    env.update({
        "LOCALDEV_MCP_CONFIG_DIR": str(temporary / "profile"),
        "RUFF_CACHE_DIR": str(temporary / "ruff"), "UV_CACHE_DIR": str(temporary / "uv"),
        "npm_config_cache": str(temporary / "npm"), "PYINSTALLER_CONFIG_DIR": str(temporary / "pyinstaller"),
        "GIT_CEILING_DIRECTORIES": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "QT_QPA_PLATFORM": "offscreen",
    })

    def run(arguments: list[str], cwd: Path = ROOT, timeout: int | None = None, *, extra_env: dict[str, str] | None = None) -> None:
        subprocess.run(arguments, cwd=cwd, env=env if extra_env is None else {**env, **extra_env}, check=True, timeout=timeout)

    run([sys.executable, "scripts/check_macos_crypto.py"], timeout=20)
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("Build-time Node/npm is required")
    engine = ROOT / "third_party" / "codexpro"
    run([npm, "ci"], engine)
    run([npm, "run", "build"], engine)
    run([sys.executable, "scripts/prepare_codexpro_runtime.py"])
    run([npm, "run", "smoke"], engine)
    prepare_runtimes(temporary, architecture)
    distribution = temporary / "dist"
    run([sys.executable, "-m", "PyInstaller", "packaging/macos.spec", "--noconfirm", "--distpath", str(distribution), "--workpath", str(temporary / "build")])
    app = distribution / "MCPDevBridge.app"
    executable = app / "Contents" / "MacOS" / "MCPDevBridge"
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=30)
    smoke_report = temporary / "native-smoke.json"
    run([str(executable), "--macos-self-test"], timeout=60, extra_env={
        "MCPDEVBRIDGE_ISOLATED_SELFTEST": "1",
        "MCPDEVBRIDGE_SELFTEST_REPORT": str(smoke_report),
    })
    smoke = json.loads(smoke_report.read_text(encoding="utf-8"))
    if (smoke.get("architecture") != architecture
            or any(smoke.get(key) != "passed" for key in ("qt", "private_runtimes", "mcp", "read_only_call"))
            or not isinstance(smoke.get("tool_count"), int) or smoke["tool_count"] < 1):
        raise RuntimeError("Native package probe did not verify the bundled MCP engine")
    native_check = {"scope": "native_frozen_probe", "release_eligible": False,
                    "source_commit": source_commit, "version": version, **smoke}
    (parent / f"native-check-{architecture}.json").write_text(json.dumps(native_check, indent=2), encoding="utf-8")
    print(json.dumps(native_check), flush=True)
    # These are still mandatory release gates. A native smoke receipt alone
    # never allows creation of a release archive or release provenance.
    run([sys.executable, "-m", "pytest", "tests", "-q", "--basetemp=" + str(temporary / "pytest")])
    run([sys.executable, "-m", "ruff", "check", "src", "tests"])
    run([sys.executable, "-m", "pyright", "--pythonpath", sys.executable, "src", "tests"])
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    filename = f"MCPDevBridge-macOS-{architecture}-{version}.zip"
    archive = release / filename
    if archive.exists():
        raise RuntimeError("Existing release asset must not be overwritten")
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)])
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (release / (filename + ".sha256")).write_text(f"{digest}  {filename}\n", encoding="ascii")
    receipt = {"version": version, "source_commit": source_commit, "architecture": architecture,
               "asset": filename, "sha256": digest, "native_frozen_smoke": "passed",
               "signature": "developer_identity" if env.get("MACOS_CODESIGN_IDENTITY") else "ad_hoc",
               "notarized": False, "published": False}
    (release / (filename + ".provenance.json")).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
