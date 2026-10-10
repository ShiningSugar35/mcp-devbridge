# MCP connection loss and Defender quarantine — 2026-10-10

Status: cause of local outage confirmed; MCP recovery NOT COMPLETED. All times below are Beijing time (UTC+08:00), unless marked UTC.

## Evidence

- Defender Operational event 1116 first detected `D:\mcp-devbridge\MCP DevBridge\MCPDevBridge.exe` at 23:38:52.543. Threat: `Trojan:Win32/Bearfoos.B!ml`, ID `2147731849`.
- Event 1116 at 23:39:19 included the EXE, GUI process 9212, elevated broker process 18584, the Start Menu shortcut, and the `MCP DevBridge Elevated Broker` scheduled task and its registry records.
- Event 1117 at 23:39:20 reports successful quarantine (error `0x00000000`); `Get-MpThreatDetection` confirms `ActionSuccess=true`, last status change 23:39:20.264.
- Live checks find the installed EXE absent and both GUI/broker processes absent. Hub port 8786 and D engine port 8789 refuse connections with Windows socket error 10061. F engine port 8792 also has no listener. C/E engine ports 8787/8790 and Windows backend ports 28731–28734 retain listeners.
- `elevation.py` configures `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` for broker-managed engines. Broker termination explains the simultaneous loss of elevated engines; this is an inference from the configured lifecycle and observed process/listener loss, not a separately captured child termination event.
- Gateway log last completed request is tools/list at 15:39:17Z: HTTP 200, 50 tools. Flight recorder ends at 15:39:19.463Z while a wait_task SSE response was starting (trace `4d1144a452004f76`); no terminal event follows. This fits abrupt process termination, rather than a normal routed error response.
- Fresh native probes: grw server_config with route 6e1b6dbf and wjp devbridge_list_devices both return JSON-RPC -32603 Internal error. These failures produce no new local Gateway entries while the Hub is absent. The local outage is established; the exact endpoint/dependency configuration of wjp has not been independently established, so its identical error is not proof of a shared origin.
- Unauthenticated GET probes of `https://jerry.shiningsugar.shop/mcp` and `https://mcp.shiningsugar.shop/mcp` both return HTTP 403 from Cloudflare. These are edge observations, not authenticated MCP initialize/tool-call results, and cannot prove whether the error came from an origin, authorization, or an edge rule.
- A direct local read-only `ssh -o BatchMode=yes -o ConnectTimeout=10 opencode-server true` exits 0. This verifies remote SSH independently; it does NOT satisfy the requested recovered-MCP SSH acceptance.

## Preserved project state

The project durable file remains at `D:\360MoveData\Users\gaoma\Desktop\应用心理实践技能大赛\.ai-bridge\long-runs\lr_mv2k622k_bd6078d401ac.json`.

- Run ID: `lr_mv2k622k_bd6078d401ac`; workspace: `ws_193f588d032aad72ce9bc3e3`.
- Status `working`, workRevision 3, last update `2026-10-10T15:39:10.871Z`.
- Task `9f61a8eb-263d-4bb9-ae97-e2b194ff747a` has a persisted durable resolution: completed, exitCode 0, signal null, finishedAt `2026-10-10T15:39:10.854Z`.
- This predates the final patch merge receipt at 15:39:17Z. It does not validate the merged patch. Post-merge TypeScript checking remains pending.
- The durable file and task state were read only; no manual edits, cleanup, cancellation, or replay occurred. Availability of other tasks' in-memory records remains unknown.

## Recovery boundary and pending acceptance

The alert may be a false positive, but that has NOT been established. Existing source/test/build receipts do not settle a malware verdict for the quarantined binary. No Defender exclusion, protection disablement, restore, replacement executable launch, service stop, container restart, task cleanup, installation, or workspace disconnection has been performed.

The retained previous-version installer `.ai-bridge/safety-20261008/rollback-5ccad41-setup.exe` was freshly verified as SHA-256 `8f9b46c9d5952f3ec5709d7482df770d2bbdc3dad8ba9e32d51b7c777d0dd635`. It was not executed. The previously recorded installed EXE hash is historical, not freshly verified, because the file is absent.

Before execution recovery: review the quarantine and file provenance using the official Windows Security/Microsoft review process. Restoring a quarantined file changes system security state and requires explicit authorization under repository AGENTS.md §1.2. Preserve the existing decision to defer the pending content-check update. Re-establish only services lost to the incident, preserving surviving engines, project catalog, credentials, routes, and existing tasks. Verify runtime paths and hashes, local and public authenticated MCP initialize, server_config, open_workspace, list_tasks, get_task, and one read-only SSH call through MCP. Do not report recovery until those calls have actual receipts.

Microsoft references:

- [Protection History in Windows Security](https://support.microsoft.com/en-us/windows/security/windows-security/protection-history-in-the-windows-security-app)
- [Address false positives and negatives](https://learn.microsoft.com/en-us/defender-endpoint/defender-endpoint-false-positives-negatives)
