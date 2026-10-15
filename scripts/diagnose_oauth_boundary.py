"""Bounded OAuth boundary evidence. No registration, consent, or valid grant is sent."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
PATHS = {"/register", "/authorize", "/consent", "/token", "/revoke"}
ERRORS = {"invalid_request", "invalid_client", "invalid_grant", "invalid_scope", "invalid_target", "unsupported_grant_type", "unauthorized_client", "access_denied", "server_error"}


def summarize_log(path: Path) -> dict:
    counts: Counter[str] = Counter()
    events: deque[dict] = deque(maxlen=40)
    total = malformed = 0
    with path.open("rb") as stream:
        for _number in range(150000):
            raw = stream.readline(65537)
            if not raw:
                break
            total += len(raw)
            if total > 64 * 1024 * 1024 or len(raw) > 65536:
                return {"complete": False, "reason": "input_budget", "counts": dict(counts), "events": list(events)}
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] not in PATHS:
                continue
            endpoint = item["path"]
            method = item.get("method")
            status = item.get("status")
            if not isinstance(method, str) or method not in {"GET", "POST", "HEAD", "OPTIONS"}:
                continue
            record = {"path": endpoint, "method": method}
            if isinstance(status, int) and 100 <= status <= 599:
                record["status"] = status
                counts[f"{method} {endpoint} {status}"] += 1
            stamp = str(item.get("ts", item.get("timestamp", "")))
            if re.fullmatch(r"[0-9T:.+Z-]{10,40}", stamp):
                record["time"] = stamp
            if isinstance(item.get("decision"), str) and item["decision"] in {"allow", "deny"}:
                record["decision"] = item["decision"]
            if isinstance(item.get("oauth_redirect"), str) and item["oauth_redirect"] in {"consent_page", "oauth_error", "client_callback", "unrecognized", "missing"}:
                record["redirect_kind"] = item["oauth_redirect"]
            if isinstance(item.get("oauth_error"), str) and item["oauth_error"] in ERRORS:
                record["oauth_error"] = item["oauth_error"]
            events.append(record)
        else:
            return {"complete": False, "reason": "line_budget", "counts": dict(counts), "events": list(events)}
    return {"complete": True, "bytes_read": total, "malformed_lines": malformed, "counts": dict(counts), "events": list(events)}


def public_probe(origin: str) -> list[dict]:
    uri = urlsplit(origin)
    if uri.query or uri.fragment or uri.username or uri.password or uri.path not in {"", "/"}:
        raise ValueError("Use an origin without a path, query or credentials")
    if uri.scheme != "https" and not (uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Use HTTPS, or an explicit loopback HTTP origin")
    results: list[dict] = []
    with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=False) as client:
        for method, endpoint in (("GET", "/.well-known/oauth-authorization-server"), ("POST", "/token")):
            row: dict = {"origin": origin.rstrip("/"), "method": method, "path": endpoint}
            try:
                data = {"grant_type": "authorization_code", "client_id": "devbridge-invalid-probe", "code": "invalid-probe"} if method == "POST" else None
                with client.stream(method, origin.rstrip("/") + endpoint, data=data, headers={"Accept": "application/json", "User-Agent": "MCPDevBridge-boundary-diagnostic"}) as response:
                    row["status"] = response.status_code
                    row["content_type"] = response.headers.get("content-type", "").split(";", 1)[0][:80]
                    row["managed_challenge"] = response.headers.get("cf-mitigated", "").lower() == "challenge"
                    body = bytearray()
                    for chunk in response.iter_bytes(4096):
                        body.extend(chunk)
                        if len(body) > 16384:
                            break
                    if len(body) <= 16384 and row["content_type"] == "application/json":
                        try:
                            payload = json.loads(body)
                        except ValueError:
                            payload = None
                        if isinstance(payload, dict):
                            code = payload.get("error")
                            if isinstance(code, str) and code in ERRORS:
                                row["oauth_error"] = code
                            if endpoint != "/token":
                                for field in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
                                    value = payload.get(field)
                                    if isinstance(value, str):
                                        value_uri = urlsplit(value)
                                        row[field + "_same_origin"] = (value_uri.scheme, value_uri.netloc) == (uri.scheme, uri.netloc)
                                row["issuer_response_required"] = payload.get("authorization_response_iss_parameter_supported") is True
                row["oauth_endpoint_reached"] = method == "POST" and row.get("oauth_error") in ERRORS
            except httpx.HTTPError as exc:
                row["transport_error"] = type(exc).__name__
            results.append(row)
    return results


def main() -> int:
    configure = getattr(sys.stdout, "reconfigure", None)
    if configure:
        configure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-log", type=Path)
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("Use a new output path inside the project")
    evidence: dict = {"observed_at": datetime.now(UTC).isoformat(), "valid_credentials_used": False}
    if args.gateway_log:
        evidence["log"] = summarize_log(args.gateway_log)
    evidence["probes"] = [row for origin in args.origin for row in public_probe(origin)]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
