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

## v0.6 Windows desktop behavior

The packaged desktop uses the Windows system tray. Closing the main window hides it to the tray by default; users can switch the close action to direct exit in Settings. Normal title-bar minimization remains a taskbar minimize.

## v0.7 Multi-Device and tunnel compatibility

A remote device can join a Hub from Cloudflare Named Tunnel, ngrok fixed address or Quick Tunnel. Quick Tunnel is appropriate for a remote development PC because its new random address is heartbeat-updated to the Hub. For the main Hub, use a fixed Cloudflare/ngrok address for long-term use; changing the Hub URL still requires updating ChatGPT and paired clients.
