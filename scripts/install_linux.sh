#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${MCPDEVBRIDGE_INSTALL_DIR:-$HOME/.local/opt/MCPDevBridge}"
LAUNCH=1
AUTOSTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir) SOURCE_DIR="$(cd "$2" && pwd)"; shift 2 ;;
    --target-dir) TARGET_DIR="$2"; shift 2 ;;
    --no-launch) LAUNCH=0; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -x "$SOURCE_DIR/MCPDevBridge" ]] || { echo "MCPDevBridge executable missing in $SOURCE_DIR" >&2; exit 3; }
case "$TARGET_DIR" in
  *$'\n'*|*$'\r'*) echo "Install path may not contain newlines" >&2; exit 4 ;;
esac
TARGET_DIR="$(realpath -m "$TARGET_DIR")"
SOURCE_DIR="$(realpath -m "$SOURCE_DIR")"
HOME_DIR="$(realpath -m "$HOME")"

# Custom paths are supported, but the installer must never turn a typo such as
# --target-dir "$HOME" into a recursive deletion. Existing non-empty targets
# are replaceable only when they already look like MCP DevBridge installs.
if [[ "$TARGET_DIR" == "/" || "$TARGET_DIR" == "$HOME_DIR" || "$TARGET_DIR" == "$HOME_DIR/.local" ]]; then
  echo "Refusing unsafe install target: $TARGET_DIR" >&2
  exit 4
fi
if [[ "$TARGET_DIR" != "$SOURCE_DIR" && -d "$TARGET_DIR" ]]; then
  first_entry="$(find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)"
  if [[ -n "$first_entry" && ! -x "$TARGET_DIR/MCPDevBridge" ]]; then
    echo "Refusing to replace non-empty directory that is not an MCP DevBridge install: $TARGET_DIR" >&2
    exit 4
  fi
fi

xdg_data_home="${XDG_DATA_HOME:-}"
[[ "$xdg_data_home" == /* ]] || xdg_data_home="$HOME/.local/share"
xdg_config_home="${XDG_CONFIG_HOME:-}"
[[ "$xdg_config_home" == /* ]] || xdg_config_home="$HOME/.config"

escape_desktop_exec() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\$}"
  value="${value//\`/\\`}"
  value="${value//%/%%}"
  printf '"%s"' "$value"
}
exec_target="$(escape_desktop_exec "$TARGET_DIR/MCPDevBridge")"

mkdir -p "$(dirname "$TARGET_DIR")"
if [[ "$SOURCE_DIR" != "$TARGET_DIR" ]]; then
  staging="${TARGET_DIR}.new.$$"
  rm -rf "$staging"
  cp -a "$SOURCE_DIR" "$staging"
  rm -rf "$TARGET_DIR"
  mv "$staging" "$TARGET_DIR"
fi
chmod 0755 "$TARGET_DIR/MCPDevBridge"
[[ -f "$TARGET_DIR/cloudflared" ]] && chmod 0755 "$TARGET_DIR/cloudflared"
[[ -f "$TARGET_DIR/_internal/runtime/node" ]] && chmod 0755 "$TARGET_DIR/_internal/runtime/node"
[[ -f "$TARGET_DIR/_internal/scripts/live_upgrade.sh" ]] && chmod 0755 "$TARGET_DIR/_internal/scripts/live_upgrade.sh"

app_dir="$xdg_data_home/applications"
mkdir -p "$app_dir"
desktop_file="$app_dir/mcp-devbridge.desktop"
cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=MCP DevBridge
Comment=Connect ChatGPT/Gemini MCP clients to local development workspaces
Exec=$exec_target
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF
chmod 0755 "$desktop_file"

autostart_dir="$xdg_config_home/autostart"
if [[ $AUTOSTART -eq 1 ]]; then
  mkdir -p "$autostart_dir"
  cp "$desktop_file" "$autostart_dir/mcp-devbridge.desktop"
  chmod 0755 "$autostart_dir/mcp-devbridge.desktop"
else
  rm -f "$autostart_dir/mcp-devbridge.desktop"
fi

desktop_dir="$HOME/Desktop"
if command -v xdg-user-dir >/dev/null 2>&1; then
  candidate="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  [[ -n "$candidate" ]] && desktop_dir="$candidate"
fi
if [[ -d "$desktop_dir" ]]; then
  cp "$desktop_file" "$desktop_dir/MCP DevBridge.desktop"
  chmod 0755 "$desktop_dir/MCP DevBridge.desktop"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$app_dir" >/dev/null 2>&1 || true
fi

if [[ $LAUNCH -eq 1 ]]; then
  nohup "$TARGET_DIR/MCPDevBridge" >/dev/null 2>&1 &
fi

echo "MCP DevBridge installed to $TARGET_DIR"
echo "Desktop entry: $desktop_file"
[[ $AUTOSTART -eq 1 ]] && echo "Autostart enabled for Linux/SteamOS Desktop Mode."
