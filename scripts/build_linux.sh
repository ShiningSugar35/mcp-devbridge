#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(python3 - <<'PY'
import tomllib
with open('pyproject.toml','rb') as f:
    print(tomllib.load(f)['project']['version'])
PY
)"
fi
python3 scripts/check_release_version.py --root "$ROOT" --expected "$VERSION"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "Missing .venv/bin/python" >&2; exit 2; }

DIST_ROOT="$ROOT/dist/staging-$VERSION"
DIST_DIR="$DIST_ROOT/MCPDevBridge"
WORK_DIR="$ROOT/build/staging-$VERSION-linux"
PACKAGE="$ROOT/release/MCPDevBridge-Linux-x86_64-$VERSION.tar.gz"

echo "== Linux 1/7 portable runtimes =="
bash scripts/prepare_runtime_linux.sh

echo "== Linux 1b/7 CodexPro build =="
( cd third_party/codexpro && npm ci && npm run build )
"$PY" scripts/prepare_codexpro_runtime.py

echo "== Linux 2/7 tests =="
QT_QPA_PLATFORM=offscreen "$PY" -m pytest tests/ -q

echo "== Linux 3/7 lint/typecheck =="
"$PY" -m ruff check src tests
"$PY" -m pyright --pythonpath "$PY" src tests

echo "== Linux 4/7 CodexPro smoke =="
( cd third_party/codexpro && npm run smoke )

echo "== Linux 5/7 PyInstaller =="
rm -rf "$DIST_ROOT" "$WORK_DIR"
"$PY" -m PyInstaller packaging/local-dev-mcp-bridge.spec \
  --noconfirm --clean --distpath "$DIST_ROOT" --workpath "$WORK_DIR"

[[ -x "$DIST_DIR/MCPDevBridge" ]] || { echo "Frozen executable missing" >&2; exit 3; }
[[ -x "$DIST_DIR/cloudflared" ]] || { echo "cloudflared missing" >&2; exit 4; }
[[ -x "$DIST_DIR/_internal/runtime/node" ]] || { echo "private Node.js missing" >&2; exit 5; }
[[ -f "$DIST_DIR/_internal/scripts/live_upgrade.sh" ]] || { echo "live_upgrade.sh missing" >&2; exit 6; }

cp scripts/install_linux.sh "$DIST_DIR/install.sh"
chmod 0755 "$DIST_DIR/install.sh" "$DIST_DIR/MCPDevBridge" "$DIST_DIR/cloudflared" \
  "$DIST_DIR/_internal/runtime/node" "$DIST_DIR/_internal/scripts/live_upgrade.sh"

echo "== Linux 6/7 frozen headless smoke =="
QT_QPA_PLATFORM=offscreen timeout 8s "$DIST_DIR/MCPDevBridge" >/tmp/mcp-devbridge-linux-smoke.log 2>&1 &
SMOKE_PID=$!
sleep 3
if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
  wait "$SMOKE_PID" || { cat /tmp/mcp-devbridge-linux-smoke.log >&2; exit 7; }
else
  kill "$SMOKE_PID" 2>/dev/null || true
  wait "$SMOKE_PID" 2>/dev/null || true
fi

echo "== Linux 7/7 package =="
mkdir -p release
rm -f "$PACKAGE"
tar -czf "$PACKAGE" -C "$DIST_ROOT" MCPDevBridge
sha256sum "$PACKAGE"
echo "Linux package: $PACKAGE"
echo "Linux build OK (version $VERSION)"
