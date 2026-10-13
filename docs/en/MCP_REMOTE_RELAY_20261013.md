# Dedicated MCP relay deployment — October 13, 2026

The owner explicitly authorized the five-minute trial, then separately authorized a persistent MCP-only relay. This changes the MCP egress path; it does not repair the computer's direct Cloudflare port-7844 connectivity or change its system proxy.

```text
MCP client -> existing Cloudflare hostname -> opencode-server cloudflared
  -> remote 127.0.0.1:8786 -> authenticated SSH reverse forward
  -> local 127.0.0.1:8786 -> existing Hub and project engines
```

The existing hostname, Cloudflare tunnel, Hub authorization, workspace routes and project permissions are retained. No Docker restart, business-service restart, task cancellation, global proxy change, firewall change, or additional Defender exclusion was performed. HTTP origin traffic, including MCP authorization headers, crosses this owner-approved server; the server is part of the connection's trust boundary. The tunnel token remains there in a restricted file. It is not placed in command arguments, Git, or diagnostic receipts.

## Installed objects and ownership

Server `opencode-server`:

- Dedicated system user/group `mcp-devbridge-relay`, no home directory, shell `/sbin/nologin`.
- `/opt/mcp-devbridge-relay/cloudflared`, root-owned mode 755, SHA-256 `9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17`. It comes from the Linux frozen payload of CI 38354638192, source 128121d.
- `/etc/mcp-devbridge-relay`, root:dedicated-group mode 750; `tunnel-token`, root:dedicated-group mode 640.
- `/etc/systemd/system/mcp-devbridge-relay.service`, enabled and started. The tested host uses systemd 219; `systemd-analyze verify` passed. Runtime user/group are the dedicated unprivileged account, `CapabilityBoundingSet=0`, `NoNewPrivileges=yes`, `ProtectHome=yes`, `ProtectSystem=full`. ICMP capability is intentionally unavailable; HTTP MCP does not require it.
- Both the reverse forward on 8786 and connector metrics on 20249 bind only to `127.0.0.1`.

Windows:

- Current-user, limited-permission task `MCP DevBridge - SSH relay`, interactive logon trigger, unlimited runtime, no parallel task instances. It uses the existing Windows OpenSSH client and `opencode-server` configuration.
- `%LOCALAPPDATA%\LocalDevMCPBridge\relay\relay-supervisor.ps1`; SHA-256 `cfa214603c2d98980ee364f42a60f80d7237838859d26132f89d5443e5e7a51a`. The source is [scripts/mcp-relay-supervisor.ps1](../../scripts/mcp-relay-supervisor.ps1).
- A bounded overwrite-only `state.json` records owned SSH PID/start time and process liveness, explicitly **not** public-MCP readiness.
- The supervisor starts a forward only while port 8786 is owned by `D:\mcp-devbridge\MCP DevBridge-0.8.9.7\MCPDevBridge.exe`; that file actually runs version 0.8.9.8. Hub exit stops its owned forward. Three unsuccessful forward attempts trigger a 60-second cooldown. No SSH/global proxy configuration is rewritten.

The service source is [scripts/mcp-devbridge-relay.service](../../scripts/mcp-devbridge-relay.service). Installed service text matches after newline normalization; the SSH text-mode transfer wrote CRLF on Linux. Installed SHA-256 is `04cc42d93cfafaba11cca0780d94d16957bc7961bff4f10110f04662a539f17f`; canonical LF source SHA-256 is `9d396e238977b56fe8e8b571d7a252a2f9b9abace98a775a474be7eb4168ebd4`. Verification and runtime checks passed with that installed file; no content difference is hidden by this normalization.

## Validation and limitations

The bounded trial passed public MCP 12/12 and native mcp-grw configuration/open/list/get/wrapper-read/SSH checks. SSH task `5691957a-f830-4034-bf37-570e25203333` completed with exit 0. The trial connector/forward stopped, its token was removed, and its precisely owned binary/PID/directory were subsequently removed after the persistent binary hash was verified.

The first persistent short-budget run passed local 12/12 and public 11/12. Its final public request initialized successfully, then timed out under an eight-second per-request limit. At that time the relay had four live connections and zero origin-request errors. A separate read-only client-path comparison retained both the proxy-enabled timeout and successful direct/proxy calls. This does not prove a specific proxy cause, and the failed receipt remains a failure.

At 14:47 Beijing time the newly created supervisor was stopped normally using its scoped `stop.request`, then its task restarted. The owned SSH PID changed from 47540 to 21472 in 11.021 seconds. Hub, engines and container were preserved. Login configuration was inspected; the computer was not rebooted or logged out to claim a real login test.

Final longer observation uses the desktop's existing 15-second request timeout, unchanged environment proxy, 21 predetermined rounds spaced 30 seconds apart. Its results, including every failure, are saved in `.ai-bridge/tunnel-20261013/permanent/ten-minute-monitor.json`; the terminal receipt is exit 0: local **21/21**, public **21/21**, elapsed **604.61 seconds**. Maximum whole-public-round time was 9.36 seconds; this is distinct from the per-request timeout. Earlier eight-second failures must not be relabeled as passes.

The GUI still owns its original local cloudflared connection and may log unsuccessful direct-connector retries. This separately managed relay does not make those direct attempts healthy or introduce a new GUI connection mode. Real public MCP diagnostics and native calls assess the relay's availability. No claim of permanent availability, repaired system networking, completed GUI relay integration, or a new public GitHub Release is made.

A subsequent identity-checked failure injection stopped only owned SSH PID 21472. The unchanged supervisor automatically established PID 55764 in **18.321 seconds**; native configuration/open/list/get/wrapper-read/readonly SSH then passed. Missing or invalid public authorization both returned HTTP 401. An initial helper string/DateTime guard rejected before mutation; matching UTC ticks were confirmed and the guard corrected.

Native mcp-wjp device status and its explicitly catalog-routed D configuration also passed. It is a different computer and was not modified. Its earlier unbound config probe returned a clear UNAVAILABLE routing error, not a transport-internal failure.

## Controlled stop and withdrawal

To stop the newly installed forward normally, create `stop.request` in its dedicated Windows relay directory and wait for its scheduled task to finish and recorded SSH process to exit. Disable only `MCP DevBridge - SSH relay` to prevent future login starts. To resume, remove that owned request file and start that same task. Do not terminate processes by broad name or stop project engines.

On the server, stop/disable only `mcp-devbridge-relay.service`. Removing its dedicated token withdraws the stored credential; do not print it or remove other Cloudflare configuration. Preserve the exact installation receipt before removing any created user or file. A future MCP installation-path change must update the supervisor's expected EXE path after identity checks; an unknown port owner is intentionally left untouched.

Runtime receipts and authorized installation inventories are retained under `.ai-bridge/tunnel-20261013/`. Existing rollback packages and previous failures remain separate. Previously policy-refused cleanup directories and unrelated untracked user files are untouched.
