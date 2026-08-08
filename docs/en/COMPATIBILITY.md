# Compatibility

## Supported environment

| Item | Requirement |
|---|---|
| OS | Windows 10/11 (x64) — PowerShell 5.1+ |
| Python | 3.12 (locked: 3.12.10) |
| UI | PySide6 6.11.1 |
| Node engine | Node.js ≥ 18 (used by the CodexPro engine) |
| MCP SDK | mcp 2.0.0 (streamable HTTP) |
| Tunnels | `cloudflared` ≥ 2024.x (tested 2026.7.3) or `ngrok` v3 |

## MCP client compatibility

- Works with any client implementing the **Streamable HTTP** transport over
  HTTPS + Bearer: ChatGPT Codex/GPT custom MCP, Gemini (MCP), Cursor, Claude Desktop (JSON config), IDE plugins.
- Endpoint shape: `POST https://mcp.<domain>/mcp`.

## Cloudflare requirements

- Zone active on your domain (e.g. `shiningsugar.shop`).
- A Named Tunnel (token-based) with a public hostname route to
  `http://localhost:8787` (or the configured origin port).
- DNS record auto-provisioned by the dashboard route (**CNAME** to
  `*.cfargotunnel.com`).

## Verified end-to-end (2026-08-08)

`https://mcp.shiningsugar.shop/mcp` — without Bearer returns **HTTP 401**
(chain: Cloudflare edge → tunnel → local engine → auth middleware).

## Notes

- Quick Tunnel URLs are temporary; for production use a named tunnel so the
  URL stays identical across restarts.
- If a corporate proxy interferes with `cloudflared`, configure it
  explicitly; the app does not yet proxy tunnel traffic.
- Python backend port (`RuntimeConfig.local_port`) defaults can be overridden
  at runtime; see 进度验收.md known issues for the 8765/2865 mismatch note.