# Security

## v0.5.0 security model

- All engines, Gateway and legacy backend bind to loopback only; only a configured tunnel is public.
- Public Cloudflare/ngrok/Quick traffic terminates at the OAuth/Bearer Gateway, never directly at CodexPro.
- Bearer and Cloudflare tunnel values are **per project** and stored in Windows Credential Manager or the DPAPI fallback; they are not plaintext fields in `projects.json`.
- Legacy shared Bearer migration is single-owner: the first compatible project can inherit the old value, while later projects receive unique credentials. This keeps direct-Bearer workspace routing unambiguous.
- Gateway comparisons use timing-safe equality. OAuth workspace routing replaces the public credential with the target project's upstream Bearer.
- With a workspace registry, Gemini consent requires an explicitly selected **running** workspace: missing selection returns 400; a stopped workspace returns 409; neither path issues an authorization code.
- Audit and diagnostic views never print protected values; sensitive parameter names remain redacted.
- Desktop new projects intentionally default to `system + full_system` (“完全访问（危险）”). The first actual use still requires the one-time risk acknowledgement. Users can explicitly reduce access to `workspace + developer` or read-only.
- The application blocks known destructive disk/system command patterns in every execution profile.
- Window shutdown performs process cleanup off the GUI thread.
- Upgrade handoff files contain only non-secret metadata; the new process reloads protected values from SecretsStore.

## v0.7 Multi-Device security

- Pairing codes are six-digit, single-use and memory-only with a ten-minute lifetime. They are never persisted.
- `devices.json` contains only device id/name, endpoint and timestamps. Remote MCP Bearer values and heartbeat secrets are stored in Windows Credential Manager / DPAPI-backed `SecretsStore`.
- Heartbeats require the per-device secret and update the remote endpoint; remote endpoints must use HTTPS (loopback HTTP exists only for local development/tests).
- The Hub swaps its own client credential for the selected remote device's Bearer only on the outbound proxy request. Device credentials are not returned through MCP tools or written to audit/network logs.
- Gateway tool audit continues using the existing redaction policy, including complete masking of command/content/patch values and secret-like key names.
- A remote device using Local-only mode cannot join a Hub because its loopback address is not reachable from another physical computer.
