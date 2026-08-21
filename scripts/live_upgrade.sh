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

xdg_config_home="${XDG_CONFIG_HOME:-}"
[[ "$xdg_config_home" == /* ]] || xdg_config_home="$HOME/.config"
CONFIG_DIR="${LOCALDEV_MCP_CONFIG_DIR:-$xdg_config_home/LocalDevMCPBridge}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_INSTALL="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)"
if [[ ! -x "$CURRENT_INSTALL/MCPDevBridge" ]]; then CURRENT_INSTALL=""; fi
TARGET_DIR="${MCPDEVBRIDGE_INSTALL_DIR:-${CURRENT_INSTALL:-$HOME/.local/opt/MCPDevBridge}}"
TARGET_DIR="$(realpath -m "$TARGET_DIR")"
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

install_args=(--source-dir "$SOURCE" --target-dir "$TARGET_DIR" --no-launch)
autostart_file="$xdg_config_home/autostart/mcp-devbridge.desktop"
[[ -f "$autostart_file" ]] || install_args+=(--no-autostart)
bash "$SOURCE/install.sh" "${install_args[@]}"
nohup "$TARGET_DIR/MCPDevBridge" >/dev/null 2>&1 &
NEW_PID=$!

EXPECTED_PORT="$("$NODE" - "$CONFIG_DIR/projects.json" "$CONFIG_DIR/upgrade-resume.json" <<'NODEJS'
const fs = require("fs");
const path = require("path");
const [projectsPath, resumePath] = process.argv.slice(2);
let projects = {}; let resume = {};
try { projects = JSON.parse(fs.readFileSync(projectsPath, "utf8")); } catch (_) {}
try { resume = JSON.parse(fs.readFileSync(resumePath, "utf8")); } catch (_) {}
const root = String(resume.project_root || "");
const rows = Array.isArray(projects.projects) ? projects.projects : [];
const match = rows.find((p) => root && path.resolve(String(p.root_path || "")) === path.resolve(root));
if (!match) process.stdout.write("0");
else if (String(match.connection || "") === "local") process.stdout.write(String(Number(match.codexpro_port) || 8787));
else process.stdout.write(String(Number(match.gateway_port) || 8786));
NODEJS
)"

ready=0
for _ in $(seq 1 75); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then break; fi
  if [[ "$EXPECTED_PORT" =~ ^[1-9][0-9]*$ ]]; then
    if "$NODE" -e 'const net=require("net");const p=Number(process.argv[1]);const s=net.createConnection({host:"127.0.0.1",port:p});const done=(ok)=>{s.destroy();process.exit(ok?0:1)};s.setTimeout(500,()=>done(false));s.once("connect",()=>done(true));s.once("error",()=>done(false));' "$EXPECTED_PORT"; then
      ready=1
      break
    fi
  else
    sleep 3
    if kill -0 "$NEW_PID" 2>/dev/null; then ready=1; break; fi
  fi
  sleep 2
done

"$NODE" - "$CONFIG_DIR/upgrade-result.json" "$TARGET_DIR" "$NEW_PID" "$ready" "$EXPECTED_PORT" <<'NODEJS'
const fs = require("fs");
const [outPath, target, pid, ready, port] = process.argv.slice(2);
fs.writeFileSync(outPath, JSON.stringify({ok: ready === "1", platform: "linux", installed_dir: target, pid: Number(pid), expected_port: Number(port) || 0}, null, 2));
NODEJS
[[ "$ready" == "1" ]] || { echo "Updated MCP DevBridge did not become ready" >&2; exit 8; }

rm -rf "$TMP"
trap - EXIT
exit 0
