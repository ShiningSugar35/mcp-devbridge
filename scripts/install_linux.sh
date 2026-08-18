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

mkdir -p "$(dirname "$TARGET_DIR")"
if [[ "$(realpath -m "$SOURCE_DIR")" != "$(realpath -m "$TARGET_DIR")" ]]; then
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

app_dir="$HOME/.local/share/applications"
mkdir -p "$app_dir"
desktop_file="$app_dir/mcp-devbridge.desktop"
cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=MCP DevBridge
Comment=Connect ChatGPT/Gemini MCP clients to local development workspaces
Exec=$TARGET_DIR/MCPDevBridge
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF
chmod 0755 "$desktop_file"

if [[ $AUTOSTART -eq 1 ]]; then
  autostart_dir="$HOME/.config/autostart"
  mkdir -p "$autostart_dir"
  cp "$desktop_file" "$autostart_dir/mcp-devbridge.desktop"
  chmod 0755 "$autostart_dir/mcp-devbridge.desktop"
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
