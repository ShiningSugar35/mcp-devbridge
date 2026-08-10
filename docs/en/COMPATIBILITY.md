# Compatibility

## Supported environment

| Item | Requirement |
|---|---|
| OS | Windows 10/11 x64, PowerShell 5.1+ |
| Python | 3.12 (development baseline 3.12.10) |
| UI | PySide6 6.11.1 |
| Node engine | Node.js 18+ |
| MCP SDK | mcp 2.0.0, Streamable HTTP |
| Public tunnels | cloudflared (tested 2026.7.3) or ngrok v3 |

## Desktop client modes

Each project selects either **ChatGPT web** or **Gemini Spark**. The Gemini OAuth panel is only visible for Gemini projects. ChatGPT/Bearer behavior remains available without Gemini static-client configuration.

## Connection methods

1. Cloudflare Named Tunnel — stable hostname, recommended for a long-lived public URL.
2. ngrok reserved/fixed domain — stable ngrok hostname; `ngrok` must be installed in PATH.
3. Quick Tunnel — temporary `trycloudflare.com` URL; it changes between runs.
4. Local — loopback only and does not require cloudflared.

All public modes end at the selected project's Gateway and expose `/mcp`; Local connects directly to CodexPro.

## Ports

Gateway/CodexPro/Windows-MCP ports are allocated per project from the defaults 8786/8787/28731 while avoiding catalog collisions. Legacy backend 8765 remains a global compatibility port. Advanced Settings edits the selected project's three primary ports and locks them while that project is running.
