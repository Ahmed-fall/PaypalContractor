---
description: Verify PayPal credentials, test the MCP server connection, and show the active sandbox environment
agent: build
---

# PayPal Setup

Verify the PayPal MCP wiring for opencode. This project connects via a **local** MCP server (`npx -y @paypal/mcp`) using the token in `~/.config/opencode/paypal_token` (interpolated in `opencode.json`).

## Workflow

### Step 1 — Check MCP connection
Count available PayPal MCP tools from the `paypal` server in this session. If present, run one lightweight read probe (e.g. list invoices with `page_size: 1`). If tools are missing, the config.json MCP entry is not loaded — restart opencode and re-check.

### Step 2 — Diagnose a failing token
Most likely causes in order:
1. **Token expired** — PayPal access tokens last up to 8 hours. Regenerate (Step 3).
2. **Stray whitespace/newline** in the token file — the `Authorization` header is rejected. Rewrite the file as a single line with no trailing newline.
3. **Gateway rate-limited (429)** — wait 1–2 minutes; do not regenerate.
4. **Network** — VPN/proxy blocking `mcp.sandbox.paypal.com`.

### Step 3 — Refresh the sandbox token
```bash
curl -X POST https://api-m.sandbox.paypal.com/v1/oauth2/token \
  -u "$PAYPAL_CLIENT_ID:$PAYPAL_CLIENT_SECRET" \
  -d "grant_type=client_credentials" | jq -r .access_token \
  | tr -d '\n' > ~/.config/opencode/paypal_token
```
Then fully quit and reopen opencode.

### Step 4 — Project environment check
Scan the project for server-side credentials (separate from MCP auth): look for `.env` / `.env.example`. Confirm `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` are present without printing their values. Never echo secrets.

### Step 5 — Summary
Report: MCP connected? token set? project env status? token expiry note (401 → `/paypal-setup refresh`).

## Error Reference
| Error | Cause | Fix |
| --- | --- | --- |
| No PayPal MCP tools | config not loaded / not restarted | restart opencode |
| 401 Unauthorized | expired/malformed token | regenerate, single line, restart |
| SSE/header invalid | newline/whitespace in token | rewrite file single-line |
| 429 | gateway rate-limit | wait 1–2 min |
| Timeout/refused | network/VPN/proxy | allowlist `mcp.sandbox.paypal.com` |
| 403 Forbidden | missing app permissions | check Dashboard app scopes |
