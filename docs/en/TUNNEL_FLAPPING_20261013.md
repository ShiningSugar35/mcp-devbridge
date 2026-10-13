# Public tunnel instability — 2026-10-13

Status: diagnosed, NOT FIXED. Times below are Beijing time (UTC+08:00).

## Current evidence

- Running desktop PID 16840 was launched on October 11 from `D:\mcp-devbridge\MCP DevBridge-0.8.9.7\MCPDevBridge.exe`. Its freshly checked SHA-256 is `9546b403e4873b8f7dd20531df9442bd3f916e3e23c71df941ef3a46dad5c6f1`, matching the previously installed CI payload. The broker and listeners on 8786/8787/8789/8790/8792 remain present. This incident is not the previous executable disappearance.
- In the 12:34–13:04 log window, all 19 recorded quick local health checks returned HTTP 200. Public health returned 200 in only 3/19 checks; the other 16 were recorded as status 0. Nine tunnel rebuilds started, four reported success and four failed, with another in progress at the snapshot. No continuous-public-availability PASS is established.
- Fresh authenticated local MCP initialize, streaming transport, tools/list and explicit D-route server_config passed. The Hub still exposes 50 tools with schema `ccd6ee228bf49f0d1319c571e5c0b9ecce00f9a5ad66d338f273883595cd547d`. Fresh public MCP initialization failed; native mcp-grw server_config returned -32603.
- Both default-environment and direct public health probes returned HTTP 530. A subsequent response was verified as Cloudflare error 1033: no healthy connector was available to Cloudflare. Existing HTTP/HTTPS proxy configuration was observed, not modified.
- Tunnel logs show repeated QUIC inactivity/handshake failures and TCP 7844 I/O timeouts. DNS prechecks passed; UDP/TCP availability and Cloudflare API reachability varied. A fresh TCP socket probe connected to 198.41.192.167:7844 in 3235 ms, while 198.41.200.63:7844 timed out after 4016 ms. A socket connection is not a successful TLS/HTTP2 tunnel handshake. API HTTP 404 proves an HTTP response at that moment, not tunnel availability or authentication success.

The established failure layer is cloudflared-to-Cloudflare connectivity. Attribution to a particular proxy, ISP, firewall or Cloudflare incident is NOT established. The Cloudflare API failure is corroborating network evidence; API access on port 443 is not equivalent to the tunnel data path on port 7844.

## Recovery mechanism findings

1. `ServiceCoordinator._restart_public_tunnel()` reports “recovered” immediately after `TunnelManager.wait_ready()`. That readiness recognizes a tunnel registration log, without requiring a subsequent authenticated public MCP probe. At 12:36:34, 12:42:30, 12:43:53 and 12:55:30 recovery was reported, followed by public health failures. A short successful registration must not be presented as sustained public recovery.
2. The cooldown timestamp is set before the rebuild. Three 90-second attempts plus backoff exceed the 60-second cooldown. Consequently, failures at 12:49:25 and 12:54:21 were followed by new rebuilds at 12:49:40 and 12:54:36. This explains repeated rounds despite the message promising a cooldown after failure.
3. `EngineManager._fail()` clears its process reference before the coordinator classifies the failure. An isolated fake-process reproduction confirmed that the prior ring-log TCP timeout disappears from `log_tail()`. The persistent tunnel log still contains the error, but coordinator events become `unknown_process_failure` with a repeated digest. No real process was started or stopped for this reproduction.

These findings affect recovery reporting, retry scheduling and diagnostic quality. They do not explain away the independently observed network failures, and repairing them alone would not establish restored public availability.

## Remaining work and acceptance

- Establish a usable tunnel transport with real TLS/HTTP2 or QUIC handshakes and repeated public MCP calls; do not assume forcing HTTP2 solves periods when TCP 7844 also fails.
- Preserve bounded failure evidence, measure cooldown after the completed attempt sequence, and distinguish connector registration from public MCP recovery. Register a repair plan and targeted regressions before implementation. Keep project engines available independently of tunnel state.
- Preserve the existing proxy, project processes, credentials and workspace routes. No production restart, installation, protocol override, proxy/firewall change, or task cancellation was performed in this diagnosis. The owner's October 11 idle/restart statement is not evidence that October 13 tasks are idle.
- Reuse network durable run `lr_muv8n18v_5213e062579d`; keep the previous failed continuous-public gate visible. Future deployment requires a fresh task drain and version-specific build/runtime receipts.

Evidence: `.ai-bridge/tunnel-20261013/network-receipt.json`, `transport-window.json`, `tunnel-errors.log`, `mcp-receipt.json`, `public-error-receipt.json`, and `failure-log-reproduction.json`. Original production logs remain unchanged.

Official references: [Cloudflare tunnel troubleshooting and 1033](https://developers.cloudflare.com/tunnel/troubleshooting/), [connectivity prechecks](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/troubleshoot-tunnels/connectivity-prechecks/). These document UDP/QUIC and TCP/HTTP2 on port 7844; they do not establish the specific cause on this machine.
