# BiliMate MCP incident — 2026-10-09

Executor: Codex, toolchain engineering session. Result: PARTIAL_FIXED.
This result describes a repaired diagnostic defect. The three original refused operations remain BLOCKED / NOT_RUN; development recovery has not been verified.

## Confirmed runtime and source

- BiliMate: D:\360MoveData\Users\gaoma\Desktop\bilimate; branch v7-data-deepening.
- Gateway project route: 6e1b6dbf. BiliMate opaque handle: ws_d0a43e79d9ab27f4a5b17cf6.
- The route belongs in devbridge_workspace_id; it is neither devbridge_device_id nor the opaque workspace_id.
- Actual executable: D:\mcp-devbridge\MCP DevBridge\MCPDevBridge.exe; PID 9212 at inspection, SHA256 24966ae18081d7da5f6ec65cb9eb2b967bec27fccac4908debdbd0ec5d558a7d.
- Verified implementation checkout: D:\360MoveData\Users\gaoma\Desktop\mcpDevBridge, starting commit 97a12ad9958fefa30471e721471881355c64e31a. D:\Environment\mcp does not exist here.
- Before modification, checkout and installed third_party/codexpro/dist/redact.js had identical SHA256 efe18b564a1963011ebadd175371140bd740d57fed55d336ac4d344e80d9b587.
- Actual server_config: loopback D engine port 8789, bashMode=full, writeMode=workspace, toolMode=full. Ordinary source writes are enabled; per-content checks still apply.
- Initial -32603 calls were this session's incorrect routing arguments. Correct project-route arguments succeeded. This was not evidence of a production transport outage.

## Separate findings

| Operation | Confirmed layer | Root cause / recovery |
| --- | --- | --- |
| R002 service_v2.py creation | CodexPro local content check | fsOps.writeTextFile calls hasSecretValue and throws the exact recorded CodexProError before filesystem mutation. The original rejected payload and matching rule were not preserved. Misclassification versus a legitimate credential hit is unresolved. File remains absent; no retry. |
| Candidate desktop demo_core.py creation | Same CodexPro local content-check entry point | Same recorded exception and shared check. Same matched rule or triggering bytes cannot be established. File remains absent; no retry. |
| Push of 7c3ca70 | UNKNOWN: host versus tool layer not established | Owner confirmed the durable checkpoint is an agent summary, not the original error. No original task ID, exit code or request receipt is available. No basis to attribute it to GitHub permissions or network. No retry through any tool, environment or route. |

The desktop write around 09:31 has a candidate transport trace 250999b3ae91438e: forwarded to an engine, SSE HTTP 200, 440 response bytes. Logs omit request content/path and response body, so this is supporting transport evidence, not a uniquely proven payload match. HTTP 200 means response delivery, not write success.
The 08:45 window has multiple run_program requests from the shared D engine and no correlatable original Push error body. A trace cannot be assigned to that Push merely from its timestamp.

Existing task 7f406865-e565-4ea9-8a35-51251db123b1 genuinely executed Push at 08:42:23 Beijing time, PID 48736, exit 0; stderr records 07e98c5..f6acdff. This predates 7c3ca70 and does not prove the refused Push recovered.
Live git ls-remote confirms remote branch f6acdffe5f7c678d454df16568d0a1615fa0bef9. Local HEAD inspected as 7c3ca7095bc83d46be94738be6acb390c72e5ad4; one commit ahead. These are not equal.

## Actual minimal repair

The existing content guard discarded the rule and input location and returned only a generic error. An independent synthetic RED regression reproduced this missing metadata.
Changed redact.ts, fsOps.ts and server.ts to return a fixed content_check result:
layer=codexpro/content_check, code=SECRET_CONTENT_BLOCKED, operation, ruleId, inputLine, inputColumn, occurredBeforeMutation=true.
Text-only clients also receive the same safe rule/location suffix.
Coordinates refer to checked input, including patch text, and use JavaScript UTF-16 positions. Only the first match in existing rule priority is reported.
All nine regular expressions, placeholder rules, detection order and redaction behavior are unchanged. No matched value, content excerpt, recoverable hash, arbitrary diagnostics, new approval bypass, allowlist relaxation or new audit system was added.
This repair does not authorize or replay any original refused operation.

## Verification

- TypeScript build: exit 0.
- Synthetic diagnostic regression: exit 0, actual stdio MCP write/edit/apply_patch/codexpro wrapper. Ordinary create/edit succeed; four refusal paths retain isError, reject before mutation and disclose no fixture value. All nine detector families, repeat calls, CRLF and placeholders checked.
- Result transport budget smoke: exit 0; existing compact result behavior retained.
- Initial RED: exit 1 for absent metadata, as expected. First GREEN attempt failed because the test required an explicit isError=false on success; MCP allows omitted false. Corrected the assertion and obtained PASS. Failed receipts retained.
- Compatibility smoke: first attempt failed because a project-contained non-Git fixture inherited the parent Git repository. This is an environment/fixture boundary issue, not evidence of a content-check defect. The corrected run sets GIT_CEILING_DIRECTORIES to this task's temporary fixture root and passed with exit 0 (Node 22.19.0). Both original failed and final passing receipts are retained.
- Receipts and local backups: .ai-bridge/bilimate-mcp-20261009/. Production executable/payload not modified.

## Deployment and preserved work

Not deployed. A live read of QQBot's workspace on the same D engine found four running tasks: f75df47e-eee4-4f9e-8f0f-81d014441062 (PID 44912), d9d9d10e-8cce-4ec7-9520-86ec384b0422 (38556), 23d2189e-d617-4af2-b587-3f1a778db121 (40228), ec142508-0722-44e0-a3da-223d0ad7b78f (9840).
Do not stop these tasks or restart the shared engine. Existing short-command patch 696a24a also remains an independently accepted, undeployed change; there is no evidence that deploying it resolves the original Push.
BiliMate's H4 staged deletion, S5 modified receipt, status/log edits, historical untracked assets, and concurrent product-input test edits were untouched.
No R001/R002/W9 implementation changes, collection, contacts, schedule creation/removal, reset/clean/stash/rebase or force push.
Added a diagnostic checkpoint to existing run lr_mv08dswr_4816b55fc2f1; did not change its steps, completion or schedules. Both original hourly polling definitions were left intact; their uninterrupted future execution is not certified by this repair.

## Normal next action

1. For the two content refusals, the maintainer needs the original request content retained within an authorized local review boundary. Inspect that exact payload without attempting a write; use the unchanged scanner plus rule/location diagnostics to decide whether an actual credential or source-code false positive triggered it. Do not rename, split or rewrite the payload to evade the refusal. No tool-supported exception/review endpoint was found in the exposed tool inventory.
2. Obtain the original Push error/request record from the invoking host before attributing the refusal. The owner confirms none is currently accessible. Explicitly keep responsibility UNKNOWN.
3. If the record explicitly identifies OpenAI safety denial, request review via OpenAI Support, including the app name mcp-grw, attempt time/timezone, conversation/request identifier if available, exact error and whether the server received the request. A local source change cannot grant host permission. Do not send credentials or sensitive conversation content. See [official app troubleshooting](https://help.openai.com/en/articles/20001497-troubleshooting-plugins-apps-in-chatgpt) and [contact support](https://help.openai.com/en/articles/6614161-how-can-i-contact-support).
4. If an administrative approval message is actually shown, use the existing account/workspace app action review controls. Generic local full permission does not override host safety, and an app-tool refresh is not evidence of lifting a request-level denial.
5. Deploy the accepted diagnostic patch only after normal task drain or an explicit decision by the owners of affected work. Only explicit normal permission for the original actions permits restoration verification.
6. The polls can continue currently permitted product-input/interaction work and read-only interface work in their original owned scopes. They must preserve the refused operations and pending commit.

The failure/result representation follows [MCP tool errors and structured content](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).

## Follow-up: independently proven metadata false positives

The maintainer reproduced credential-free TOKENIZER_NAME and MAX_TOKENS assignments being rejected and redacted. Source 0ad900b fixes this generic identifier-substring defect; f2c2b4b strengthens the existing durable callback smoke to distinguish genuine persistence retries from callbacks after success. These are new independent fixtures, not the original denied inputs. Full tests and isolated real HTTP ordinary configuration round-trips / credential rejection passed; final build/deployment status remains in 进度验收.md and the durable run lr_mv0bzwqp_3a1306a2beb6.

This proves a detector false-positive class exists. It does not identify the two original matched rules, restore the absent services, explain the original Push, or authorize a replay. The original three blocked operations remain unresolved.
