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

## Authorized recovery update — 2026-10-11 00:18

The preceding investigation snapshot is retained as history. The user subsequently restored the old executable manually, authorized a new version, and explicitly requested a Defender whitelist. The restored file was freshly verified against the recorded old SHA-256 (`24966ae18081d7da5f6ec65cb9eb2b967bec27fccac4908debdbd0ec5d558a7d`).

- New maintenance source: `d8e1c3b76b0cd41f18a06bedb81e75f2982650f1`, version 0.8.9.7. Includes the previously tested command/content diagnostic and ordinary-configuration false-positive repairs, plus Windows executable version/product resources. Metadata is not a code signature and does not establish that the old alert was a false positive.
- Fresh local gates: pytest 708 passed / 3 skipped; Ruff, Windows/Linux Pyright and TypeScript build exit 0; complete npm smoke exit 0 (186.094 seconds). The first local build failed because inherited PowerShell 7 module paths confused Windows PowerShell 5.1. Only the build child's module environment was corrected; repeat Windows build exit 0 (306.109 seconds). Both receipts are retained.
- Same-source CI [38066123655](https://github.com/ShiningSugar35/mcp-devbridge/actions/runs/38066123655): Windows and Linux success; Windows pytest 708/3, Linux pytest 699/12. Canonical installer and Linux archive came from this CI, independently of the local rebuild.
- Frozen CI verification: 3 policy checks, 7 content checks, version/product resources and an isolated eight-second GUI smoke. CI EXE SHA-256 `9546b403e4873b8f7dd20531df9442bd3f916e3e23c71df941ef3a46dad5c6f1`; Windows installer SHA-256 `250d05b4c7e950c7ad0d8390ab23c2ed7cdbeb785bb2790a5b80839c78d96503`; Linux archive SHA-256 `853350f73f079119413ea04a96e0671a9ddf13f1922f2c1220288d46b9f15b69`.
- Defender custom scans of the CI EXE and installer **outside exclusion paths** both completed with no threats and exit 0.
- With explicit user authorization and Windows administrator confirmation, exact-file exclusions were added and verified for `D:\mcp-devbridge\MCP DevBridge\MCPDevBridge.exe` and `D:\mcp-devbridge\MCP DevBridge-0.8.9.7\MCPDevBridge.exe`. Real-time protection remains enabled. No directory or process exclusion was added. The first UAC request was cancelled; the user explicitly requested the second attempt, which succeeded.
- New version installed into `D:\mcp-devbridge\MCP DevBridge-0.8.9.7` using the CI installer, exit 0. All 2713 CI payload files match installed hashes. The separate directory and installer no-close/no-restart flags preserve surviving old service files/processes. Original C/E Node PIDs 18100/17144 and Windows backend listeners remain intact. The current old GUI PID 49864 is idle; it has not been stopped.
- New-version elevated broker registration UAC was cancelled. The user was asked to choose another confirmation or manual startup from the new GUI. Hub/D/F are still offline; no claim of recovered MCP or completed release is made. New version tag/Release publication remains pending live acceptance.

Evidence is retained under `.ai-bridge/incident-20261010/`, including Defender event records, exclusions, scans, independent gate receipts, CI logs, installation logs and payload manifest. The existing maintenance durable run `lr_mv0bzwqp_3a1306a2beb6` is updated without modifying the psychology project's run. Existing policy-blocked cleanup directories remain untouched.

## Recovery and user-requested exit — 2026-10-11 00:36

The user authorized C/E and then full MCP restart, confirming no projects were running. Registered/default C/E task checks found no active commands and their complete process-descendant snapshots contained only conhost; full C/E workspace inventory was unavailable in standard mode. D/F full workspace inventories had no active tasks. Verified old C/E engines were stopped; the desktop, its owned tunnel and authenticated broker-managed engines were restarted without changing Docker or proxy settings. New C/D/E/F services were confirmed READY from the installed 0.8.9.7 payload. Native root configuration, psychology-project open/list/get and read-only SSH task f5e01f51-dec2-4217-ad87-ca8614505775 passed (exit 0). Fresh post-restart authenticated SDK checks passed local 3/3 and public 3/3 with 50 tools and the expected schema fingerprint. A new ordinary TOKENIZER_NAME/MAX_TOKENS fixture passed production wrapper write/edit/patch/read; original refused payloads were not replayed. mcp-wjp still returned -32603 and its endpoint remains unverified. The old TypeScript task is absent from the restarted in-memory registry; its pre-merge durable terminal receipt does not establish post-merge validation.

The main window could not be displayed. Hidden-window startup is a suspected cause, not a verified fix. Computer Use capture timed out and the user subsequently stopped Computer Use with Escape. The user then explicitly requested a complete MCP exit and a desktop shortcut for manual startup. At 00:36:46 the MCP GUI, all root engines, elevated broker, installation-owned tunnels and Windows backends were stopped; no MCPDevBridge process or listeners on 8786/8787/8789/8790/8792/28731–28734 remained, and the broker task was Ready. Unrelated system services/tunnels were preserved. The desktop MCP DevBridge.lnk targets D:\mcp-devbridge\MCP DevBridge-0.8.9.7\MCPDevBridge.exe with empty arguments and normal window style. Its EXE hash remains 9546b403e4873b8f7dd20531df9442bd3f916e3e23c71df941ef3a46dad5c6f1. No automatic restart was performed. Current runtime is intentionally STOPPED for the user; tag/Release and overall maintenance completion remain pending. Receipts: recovery-after-restart.json, orphan-drain-receipt.json, new-engines-drain-receipt.json, runtime-restart-receipt.json, manual-start-shortcut-receipt.json and fully-exited-receipt.json under the incident evidence directory.
