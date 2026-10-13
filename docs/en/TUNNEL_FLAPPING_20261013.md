# Public tunnel instability — 2026-10-13

Status: public MCP RESTORED through the owner-authorized dedicated relay. Direct local port-7844 connectivity remains unavailable; earlier failures are retained. Times below are Beijing time (UTC+08:00).

## Initial evidence (before repair)

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


## First local repair deployment (14:17, before relay)

The recovery mechanism findings above and a further desktop-diagnostics false-recovery label have been repaired. Registration now means pending verification; only the existing authenticated, explicitly routed public MCP canary confirms recovery. The cooldown begins after the full retry round. Failed-process redacted log tails are bounded to 160 lines / 16 KiB. A complete structured precheck may recommend HTTP2 only when both UDP regions fail and TCP succeeds in at least one region. Tool `isError` is a failure even if text is present.

Final candidate source `128121d8c592446b36f6aaee8251f84bb772b732` passed both platform builds in [CI 38354638192](https://github.com/ShiningSugar35/mcp-devbridge/actions/runs/38354638192): Windows 722 passed / 3 skipped, Linux 713 passed / 12 skipped. Frozen payload/security/recovery checks and Defender scans passed. Installer SHA-256: `74b5725638200c2442d10a8a020dcd1f1c6dcb191e8d17ad1f297d92861126a6`.

After a fresh idle drain, installation succeeded at 14:17. The existing registered installation directory ending in `0.8.9.7` is intentionally retained; the actual file version is **0.8.9.8**. All 2,709 manifest files match. EXE SHA-256: `e96d6d4471d7519b12a2572dd9ae91c30b9f9e3e3bec4d0ded42e73b7e4018ea`. GUI PID 9628 was launched normally, with a window title/handle and normal-window desktop shortcut. Independent visual automation was unavailable. All four engines and Hub resumed; configuration and project files are byte-identical to the pre-install backup. Restricted C/E inventory coverage is not described as a complete scan.

The final repeated real contract result is **local 12/12, public 0/12**, executor exit 1. An earlier probe before listeners were ready failed with ConnectError and remains archived. HTTP2 alone also failed in a separate real connector comparison. The software repair does not establish a healthy public tunnel. No new tag or GitHub Release was published; the durable run remains open.

Read-only network probes on `opencode-server`, inside the existing running container, verified TLS 1.3 to both Cloudflare regions using the official pinned cloudflared root CA and normal certificate verification. This is a candidate egress path, not MCP acceptance. No remote connector, credential transfer, container restart, or system proxy change has occurred. A bounded five-minute MCP-only SSH reverse-forward/remote-connector trial has been prepared locally; transferring the tunnel credential requires separate owner authorization under AGENTS.md section 1.2. Its dedicated process is time-limited and identity-checked; it removes the temporary token afterward.

Final receipts: `.ai-bridge/tunnel-20261013/final2/`; same-source unpublished assets and provenance: `release/ci-0.8.9.8-rev2/`. The first version-8 source `7912f81`, first deployment, original failures, rollback and configuration backup remain separate historical evidence. No previously refused cleanup or business mutation was retried.


## Authorized persistent relay acceptance (15:00)

After a separate owner-approved trial passed 12/12, the owner explicitly authorized persistent deployment. A dedicated unprivileged cloudflared service on opencode-server and a limited-permission Windows SSH supervisor task are now installed. The first persistent eight-second-budget run was 11/12 public and remains a failure; a later **predetermined 21-round, 604.61-second observation using the desktop's existing 15-second budget** passed local 21/21 and public 21/21 with terminal exit 0. Automatic recovery of the owned SSH forward took 18.321 seconds. Actual native server_config/open_workspace/list_tasks/get_task/wrapper-read/SSH all passed afterward; unauthenticated and invalid-authorized requests returned 401.

This restores the MCP route through a different approved egress path. It does not repair the computer's direct network or claim unlimited stability; GUI local-connector retries may still appear because no GUI relay mode was added. Proxy, project configuration, engines and Docker were preserved during relay deployment. See [deployment, exact objects, trust boundary and withdrawal](MCP_REMOTE_RELAY_20261013.md). The bounded-trial processes, token and exact trial files/directory were removed; persistent objects remain as authorized. No public tag/Release was created.
