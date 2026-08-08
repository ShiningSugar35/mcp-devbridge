# Security

## Threat model

The bridge publishes a local development server to the Internet. The threat
surface is: token theft, DNS rebinding, auth bypass, secret leakage in logs,
and remote command execution via exposed tools.

## Controls

- **Loopback binding** — the Node engine and Python backend listen only on
  `127.0.0.1`; the tunnel process is the only public entry.
- **DNS rebinding protection** — always enabled; the whitelist adds only the
  configured public hostname. Requests with a different `Host` get 421.
- **Bearer auth for public traffic** — requests seen from Cloudflare
  (`cf-connecting-ip`/`x-forwarded-for`) require a valid Bearer token;
  timing-safe comparison (`hmac.compare_digest`). Loopback can be anonymous.
- **Rate limiting** — 10 failed auth attempts per client IP → 429 locked for
  5 minutes (`AuthRateLimiter`).
- **Secret storage** — tokens live in Windows Credential Manager (DPAPI
  fallback file `secrets.dpapi.json`); never stored in plaintext JSON.
- **Log redaction** — parameter values for `content`, `old_text`, `new_text`,
  `patch`, `environment`, `command` are masked at write time; any parameter
  whose name contains KEY/TOKEN/SECRET/PASSWORD/COOKIE/AUTH is masked
  (case-insensitive); log UI shows only redacted summaries.
- **Git settings guard** — user/name/email values may not contain
  whitespace, quotes, control chars or shell metacharacters.
- **Quick Tunnel warning** — UI requires explicit confirmation (`Quick` gives
  a temporary changing URL).
- **`system` permission mode** — first activation requires a one-time risk
  acknowledgment.

## Operational notes

- Keep the tunnel token and Bearer token private; never paste them into
  chat or commit them.
- Rotate the Bearer via the desktop "重新生成令牌" button — old token is
  dropped immediately.
- When hosting public, always keep `require_public_bearer: true`.
- Cloudflare certs/credentials are never written to disk in plaintext by the
  app (tunnel token is held in memory only and passed to cloudflared).