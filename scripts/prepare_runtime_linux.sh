#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools/linux"
NODE_VERSION="22.19.0"
CLOUDFLARED_VERSION="2026.7.3"
mkdir -p "$TOOLS"

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) NODE_ARCH="x64"; CF_ASSET="cloudflared-linux-amd64" ;;
  aarch64|arm64) NODE_ARCH="arm64"; CF_ASSET="cloudflared-linux-arm64" ;;
  *) echo "Unsupported Linux architecture: $arch" >&2; exit 2 ;;
esac

need_download_node=1
if [[ -x "$TOOLS/node" ]]; then
  current="$($TOOLS/node --version 2>/dev/null || true)"
  [[ "$current" == "v$NODE_VERSION" ]] && need_download_node=0
fi
if [[ $need_download_node -eq 1 ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  archive="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  base="https://nodejs.org/dist/v${NODE_VERSION}"
  curl --fail --location --retry 3 "$base/$archive" -o "$tmp/$archive"
  curl --fail --location --retry 3 "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  expected="$(awk -v f="$archive" '$2 == f {print $1}' "$tmp/SHASUMS256.txt")"
  actual="$(sha256sum "$tmp/$archive" | awk '{print $1}')"
  [[ -n "$expected" && "$actual" == "$expected" ]] || { echo "Node.js SHA256 mismatch" >&2; exit 3; }
  tar -xJf "$tmp/$archive" -C "$tmp"
  cp "$tmp/node-v${NODE_VERSION}-linux-${NODE_ARCH}/bin/node" "$TOOLS/node"
  chmod 0755 "$TOOLS/node"
  rm -rf "$tmp"
  trap - EXIT
fi

echo "Node.js runtime ready: $TOOLS/node ($($TOOLS/node --version))"

need_download_cf=1
if [[ -x "$TOOLS/cloudflared" ]]; then
  current="$($TOOLS/cloudflared --version 2>/dev/null || true)"
  [[ "$current" == *"$CLOUDFLARED_VERSION"* ]] && need_download_cf=0
fi
if [[ $need_download_cf -eq 1 ]]; then
  url="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/${CF_ASSET}"
  curl --fail --location --retry 3 "$url" -o "$TOOLS/cloudflared"
  chmod 0755 "$TOOLS/cloudflared"
fi

echo "cloudflared runtime ready: $TOOLS/cloudflared ($($TOOLS/cloudflared --version | head -n 1))"
echo "Linux portable runtime preparation OK."
