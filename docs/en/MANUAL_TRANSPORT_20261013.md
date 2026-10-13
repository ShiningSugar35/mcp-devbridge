# Manual direct / relay connections — 0.8.9.9

The installed Windows application now offers **本机直连 / 服务器中转** under **工作台 → 公网连接路径（这台电脑共用）**. Select a path and press **切换连接路径**. The selection is shared across project roots. An idle selection applies on the next connection start; a running selection changes only the public transport and saves the preference after actual MCP verification. Failed changes attempt to restore and reverify the previous path; an unverified rollback is displayed as uncertain.

Current verified path is **服务器中转**. Direct Cloudflare connectivity on this computer still times out. The application cannot repair that underlying network simply by selecting direct mode.

## Implementation and ownership

Direct mode runs the existing local Cloudflare connector. Relay mode stops that connector and controls only the previously owner-authorized `MCP DevBridge - SSH relay` Windows task and `mcp-devbridge-relay.service` on `opencode-server`. Public traffic reaches the dedicated server connector, then its loopback origin through the authenticated SSH reverse forward to the local Hub. No system proxy, Docker, project permission or business task is changed by a transport switch.

Task user, privilege level, executable, exact arguments and supervisor SHA are checked. The remote unit SHA is pinned. Unknown or incomplete deployments fail explicitly. This release does not provision a relay, add credentials, or silently enable one on Linux. See [the deployment record](MCP_REMOTE_RELAY_20261013.md) for existing restricted objects and withdrawal instructions.

Mode transactions and health recovery are serialized. Connector liveness alone does not establish readiness: an explicit running-project route and the same access authorization must pass the real public MCP probe. The Hub and engines remain alive during switching; network clients can briefly lose connectivity.

## Verified installation and tests

- Application source: `410df1f608b14a944cd95eb7d95049827df69925`; [Windows/Linux CI 38381107745](https://github.com/ShiningSugar35/mcp-devbridge/actions/runs/38381107745), both successful. Windows: 740 passed / 3 skipped; Linux: 731 passed / 12 skipped. Local final full pytest: 740 passed / 3 skipped; lint and both platform type checks passed. Same-source full engine smoke exited 0 in 188.313 seconds.
- Packaged policy 3, content 7 and frozen transaction 9 checks passed, plus an eight-second isolated GUI launch. The first frozen helper lacked its project-local temporary directory; it was corrected before these checks passed. Windows installer and candidate EXE scans found no threats.
- Installer SHA-256: `fbaff89516f7681fdd0659be1fb2e7e35d49ab74005c40b27a1ec4cb4a9ba1ad`. Installed EXE SHA-256: `3d9c81f6266cbaf173d07d4751b689a25fec7dac6c13c8cee9a1a973be2c2d2c`; all 2709 payload files match. The existing `D:\mcp-devbridge\MCP DevBridge-0.8.9.7` directory deliberately remains; the PE version is 0.8.9.9. Fresh drain found no running/cancelling task; the installation restarted MCP's own components and restored C/D/E/F.
- The normal-window desktop shortcut points to that verified EXE. `projects.json` and encrypted secret storage remain byte-identical to the pre-install backup; only `cloudflare_transport=relay` changed in application configuration. C/E developer and D/F full administrator modes passed actual calls. Both pre-existing exact EXE Defender exclusions were confirmed through the authorized administrator engine; a standard-user query could not see them. No exclusion was added or broadened.

## Real switching acceptance

Native Windows-MCP UI actions selected direct and pressed the installed application's switch button. The dedicated remote unit became inactive/disabled and the local relay task disabled before the application's local connector ran. Two bounded direct attempts failed with `transient_network_or_dns`; the application restored relay automatically. This is a verified failure/rollback case, not a successful direct-network connection.

Hub PID 51124 and engine PIDs C=33696, D=32132, E=12592, F=16496 remained unchanged across all 38 observation samples and before/after comparisons. Local MCP remained available during the public outage. Restored relay had an active/enabled unit, readiness HTTP 200, a running owned forward, and no local connector belonging to this GUI. Native server_config/open_workspace/list_tasks/get_task/wrapper-read/Windows backend/device calls passed; a new readonly SSH task `29139620-fafc-4089-a553-2e9b6a191d9f` completed with exit 0. Missing and incorrect public authorization both returned 401.

Installation and post-rollback observations each passed six local/public rounds using the existing 15-second request budget. These bounded windows do not guarantee permanent availability. Healthy direct-network switching could not be exercised on this physical network; positive switching and unsuccessful rollback cases additionally passed isolated source and packaged-code regressions.

## Development-test incident and preserved evidence

The user-reported internal failure was reproduced while the production relay task and service were disabled. Restoring only the validated existing relay recovered native calls without restarting project engines. Two legacy tests cleared the session configuration environment instead of restoring it; an ordered regression reproduced fallback to the real application directory. This could expose later connection tests to production relay controls. The tests now use automatic environment restoration; every test reestablishes its sandbox, and real relay subprocesses are forbidden unless explicitly mocked. A complete subsequent local suite ran alongside six real local/public MCP rounds without disabling the relay.

The failed isolation regression, prior default-temp cleanup failure and later successful receipts remain distinct. The earlier targeted test bodies passed 15 checks but exited 1 on access-denied cleanup of an unknown system `pytest-current`; that run is not a PASS and that path was not removed or its permissions changed. A historical in-memory task query returned `INVALID_ARGUMENT`; it was not relabeled a transport success or replayed. Historical business writes, pushes, remote training and TypeScript work were not rerun.

Evidence is retained in `.ai-bridge/manual-switch-20261013/final3/`, with the installer/configuration backup and same-source assets under `release/ci-0.8.9.9-final/`. Automatic approval rejected this task's project-local temporary-directory cleanup with `blocked by policy` before process creation. The directories listed in `final3/cleanup-audit.json` remain; no alternate deletion tool/path was used. Unknown or previously policy-refused temporary locations and unrelated untracked files were preserved. Formal public release verification is recorded separately in the acceptance log.

Formal publication remains pending: the source-matched annotated tag exists, but the installer/archive upload showed no completion within the bounded ten-minute window. Only the verified checksum/provenance assets are in the retained draft. The owned upload helper was stopped after identity checks; no public Release is claimed. This does not affect the verified local installation.
