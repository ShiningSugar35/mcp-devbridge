# Compatibility

## Supported environments

| Item | Current v0.8.3 support |
|---|---|
| Windows | Windows 10/11 x64 desktop release; PowerShell 5.1+ is supported. |
| Linux | x86_64 desktop build and user-level installer; release CI/build baseline is Ubuntu 22.04. |
| SteamOS | Desktop Mode is supported through the Linux user-level packaging path. The application does not require writing the immutable system base. |
| Python | 3.12 development baseline. |
| UI | PySide6 6.11.x. |
| Node engine | Packaged releases provide a private Node runtime; source development requires a compatible Node/npm toolchain. |
| MCP transport | Streamable HTTP through CodexPro/Gateway. |
| Public tunnels | Cloudflare Named Tunnel, ngrok reserved/fixed domain, or Cloudflare Quick Tunnel. |

## Client modes

Each project can be configured for ChatGPT-compatible Bearer use or Gemini OAuth settings. In public Hub mode, OAuth authorizes the Hub rather than an “entry workspace”; active-root routing happens from the actual tool call.

Client/platform capabilities still apply. MCP DevBridge does not bypass a client’s plan limits, action approval rules, or write restrictions.

## Multi-root behavior

All READY roots on a device are active at the same time. Descendants inherit the permission boundary of their running root, so registering a drive root makes its descendant Git and non-Git directories addressable without registering each child project.

Absolute paths route to the most specific containing active root. Relative paths auto-route only when they uniquely identify one root; otherwise the client must provide an absolute path. `task_id` and opaque CodexPro workspace handles provide follow-up affinity but do not override stronger path evidence.

## Connection methods

1. **Cloudflare Named Tunnel** — stable hostname, recommended for a long-lived main Hub URL.
2. **ngrok reserved/fixed domain** — stable ngrok hostname; source installs require ngrok to be available separately.
3. **Quick Tunnel** — temporary `trycloudflare.com` URL; it changes when the tunnel is rebuilt and is useful for testing or remote-device backhaul.
4. **Local** — loopback-only compatibility mode that connects directly to the selected CodexPro engine.

All public modes terminate at the OAuth/Bearer Gateway. `Local` is intentionally single-engine/direct; use a public Hub/Gateway path when one MCP address must route across multiple active roots.

## Ports

Gateway, CodexPro, and optional Windows-MCP ports are allocated per project from the configured defaults while avoiding catalog collisions. Legacy backend compatibility remains separate. Port changes are locked while the affected project is running.

The shared public Hub lifecycle may be bootstrapped from one running project, but that project has no routing priority over other active roots.

## Windows desktop and installer

The packaged Windows desktop uses the system tray. Closing the main window hides it to the tray by default unless the user changes the close behavior; normal title-bar minimization stays a taskbar minimize.

The Inno Setup release is per-user and explicitly keeps the destination-directory page available, so users may install outside the default location. Packaged private Node/uv/uvx/cloudflared runtimes are resolved before system PATH where applicable.

## Linux / SteamOS installation

The Linux release asset is `MCPDevBridge-Linux-x86_64-<version>.tar.gz`. The bundled `install.sh` defaults to:

```text
~/.local/opt/MCPDevBridge
```

A custom user-writable path can be selected with `--target-dir`. The installer canonicalizes the target, refuses dangerous roots such as `/`, `$HOME`, and `$HOME/.local`, and refuses to replace an unrelated non-empty directory.

Valid absolute `XDG_CONFIG_HOME` and `XDG_DATA_HOME` values are respected. Relative XDG base-directory values are invalid by specification and are ignored in favor of the standard user defaults. Desktop-entry `Exec=` values are escaped for paths containing spaces and reserved characters.

The Linux/SteamOS secret path prefers a desktop Secret Service and uses an AES-GCM encrypted user-level fallback when no service is available.

## Multi-device compatibility

A remote device can join a main Hub using an externally reachable MCP endpoint from Named Tunnel, ngrok, or Quick Tunnel. Quick Tunnel is practical for a remote development machine because its changing URL can be heartbeat-updated to the Hub. The main Hub should use a stable Cloudflare/ngrok URL when the client configuration must remain unchanged.

Device routing and active-root routing are independent. After a remote device is selected, that device applies its own active-root/path/task routing policy.

## Release-build compatibility note

PyInstaller Linux artifacts should be built on a sufficiently old supported Linux baseline because the frozen bundle does not make glibc backward-compatible. The release workflow therefore uses Ubuntu 22.04 rather than an arbitrarily newer runner image.


## Shared local/public routing

Local and public connections use the same shared Gateway routing semantics. Local skips only the public Tunnel; it does not bind the client to one project port. The Gateway port belongs to AppConfig/Hub rather than ProjectConfig.
