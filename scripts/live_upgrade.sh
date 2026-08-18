#!/usr/bin/env bash
set -euo pipefail

PACKAGE=""
OLD_PID=""
PROJECT_ROOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --package) PACKAGE="$2"; shift 2 ;;
    --old-pid) OLD_PID="$2"; shift 2 ;;
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$PACKAGE" ]] || { echo "Upgrade package missing: $PACKAGE" >&2; exit 3; }
[[ "$PACKAGE" == *.tar.gz ]] || { echo "Expected .tar.gz Linux package" >&2; exit 4; }

CONFIG_DIR="${LOCALDEV_MCP_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/LocalDevMCPBridge}"
TARGET_DIR="${MCPDEVBRIDGE_INSTALL_DIR:-$HOME/.local/opt/MCPDevBridge}"
mkdir -p "$CONFIG_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$PACKAGE" -C "$TMP"
SOURCE="$TMP/MCPDevBridge"
[[ -x "$SOURCE/MCPDevBridge" ]] || { echo "Package payload missing MCPDevBridge" >&2; exit 5; }
NODE="$SOURCE/_internal/runtime/node"
[[ -x "$NODE" ]] || { echo "Package payload missing private Node.js" >&2; exit 6; }

# Capture every currently listening project before the old process tree is
# stopped.  This mirrors the Windows updater and keeps multi-project sessions
# alive across SteamOS/Linux upgrades without storing any secret in the handoff.
if [[ -f "$CONFIG_DIR/projects.json" ]]; then
  "$NODE" - "$CONFIG_DIR/projects.json" "$CONFIG_DIR/upgrade-resume.json" "$PROJECT_ROOT" <<'NODEJS' || true
const fs = require("fs");
const net = require("net");
const [projectsPath, outPath, requestedRoot] = process.argv.slice(2);
let payload = {};
try { payload = JSON.parse(fs.readFileSync(projectsPath, "utf8")); } catch (_) {}
const projects = Array.isArray(payload.projects) ? payload.projects : [];
function listening(port) {
  return new Promise((resolve) => {
    if (!Number(port)) return resolve(false);
    const socket = net.createConnection({host: "127.0.0.1", port: Number(port)});
    let done = false;
    const finish = (value) => { if (done) return; done = true; socket.destroy(); resolve(value); };
    socket.setTimeout(300, () => finish(false));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}
(async () => {
  const roots = [];
  for (const project of projects) {
    if (await listening(project.codexpro_port)) roots.push(String(project.root_path || ""));
  }
  const requested = String(requestedRoot || "");
  if (requested && !roots.includes(requested)) roots.unshift(requested);
  const primary = requested || roots[0] || "";
  if (primary) fs.writeFileSync(outPath, JSON.stringify({project_root: primary, project_roots: roots}, null, 2));
})();
NODEJS
elif [[ -n "$PROJECT_ROOT" ]]; then
  "$NODE" - "$CONFIG_DIR/upgrade-resume.json" "$PROJECT_ROOT" <<'NODEJS'
const fs = require("fs");
const [outPath, root] = process.argv.slice(2);
fs.writeFileSync(outPath, JSON.stringify({project_root: root, project_roots: [root]}, null, 2));
NODEJS
fi

kill_tree() {
  local pid="$1"
  local child
  while read -r child; do
    [[ -n "$child" ]] && kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill -TERM "$pid" 2>/dev/null || true
}

if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
  kill_tree "$OLD_PID"
  for _ in $(seq 1 40); do
    kill -0 "$OLD_PID" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$OLD_PID" 2>/dev/null; then
    kill -KILL "$OLD_PID" 2>/dev/null || true
  fi
fi

bash "$SOURCE/install.sh" --source-dir "$SOURCE" --target-dir "$TARGET_DIR" --no-launch
nohup "$TARGET_DIR/MCPDevBridge" >/dev/null 2>&1 &
NEW_PID=$!

"$NODE" - "$CONFIG_DIR/upgrade-result.json" "$TARGET_DIR" "$NEW_PID" <<'NODEJS'
const fs = require("fs");
const [outPath, target, pid] = process.argv.slice(2);
fs.writeFileSync(outPath, JSON.stringify({ok: true, platform: "linux", installed_dir: target, pid: Number(pid)}, null, 2));
NODEJS

rm -rf "$TMP"
trap - EXIT
exit 0
