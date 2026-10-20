---
description: Show PayPal sandbox setup, credentials, and developer dashboard resources
agent: build
---

Show PayPal sandbox developer setup. If "$ARGUMENTS" matches a recognized topic, show only that section. If empty or unrecognized, show all sections.

Topic aliases:
- "Getting Started" → setup, start, begin, quickstart, intro
- "Sandbox vs Production Credentials" → credentials, creds, auth, keys, secret, token
- "Postman" → postman, collection, api client

## Getting Started
1. Create a Developer account at https://developer.paypal.com
2. **Dashboard → Apps & Credentials → Sandbox** — create an app for Client ID + Secret
3. **Dashboard → Sandbox → Accounts** — create buyer + seller test accounts
4. Sandbox base URL: `https://api-m.sandbox.paypal.com`
5. Sandbox MCP endpoint: `https://mcp.sandbox.paypal.com/sse`

## Sandbox vs Production Credentials

| | Sandbox | Production |
| --- | --- | --- |
| API base URL | `api-m.sandbox.paypal.com` | `api-m.paypal.com` |
| Buyer approval | `sandbox.paypal.com` | `paypal.com` |
| Credentials | Sandbox app in Dashboard | Live app in Dashboard |

Never mix sandbox and production credentials. This project's MCP server connects to sandbox only (`mcp.sandbox.paypal.com`).

## Postman
PayPal publishes an official Postman collection: https://www.postman.com/paypal. Set `base_url` = `https://api-m.sandbox.paypal.com`, then run **Get Access Token** first.

## Key Resources
- Developer Dashboard: https://developer.paypal.com/dashboard
- Sandbox accounts: https://developer.paypal.com/dashboard/accounts
- Webhooks Simulator: https://developer.paypal.com/dashboard/webhooksSimulator
- API Reference: https://developer.paypal.com/api/rest/
- JS SDK reference: https://developer.paypal.com/sdk/js/reference/

For scenario testing see `/paypal-test-accounts`.
