---
description: Explain a PayPal API error code or message and provide actionable solutions
agent: build
---

If "$ARGUMENTS" is empty, respond only with: "Please provide a PayPal error code or message. Example: `/paypal-explain-error INSTRUMENT_DECLINED`" and stop.

If "$ARGUMENTS" does not match any known PayPal error code or message pattern, respond with: "Unknown error code or message: `$ARGUMENTS`. This may be a typo, a third-party error, or a new PayPal error not yet in this reference. Check https://developer.paypal.com/api/rest/reference/orders/v2/errors/ for the full list." Then explain what the string suggests based on its name, and stop — do not fabricate retry behavior or related errors.

Otherwise, explain the PayPal error "$ARGUMENTS". Provide:

1. **What it means** — plain-language explanation
2. **Common causes**
3. **How to fix it** — specific, actionable steps
4. **Code example** — detect the user's language from open files (default Node.js); show proper error handling including user-facing message and whether to retry.
5. **Related errors** — only from the reference below.

Key error reference:
- `INVALID_REQUEST` (400) — malformed body/missing fields; inspect `details[]` for `field`/`issue`
- `UNAUTHORIZED` (401) — expired/missing token; refresh via `POST /v1/oauth2/token`
- `NOT_AUTHORIZED` (401) — valid token, missing scope
- `PERMISSION_DENIED` (403) — account not eligible for this API
- `RESOURCE_NOT_FOUND` (404) — ID missing or wrong account
- `DUPLICATE_TRANSACTION` (409) — idempotency key already succeeded; retrieve existing resource
- `INSTRUMENT_DECLINED` (422) — do not retry the same instrument
- `TRANSACTION_REFUSED` (422) — risk decline; do not auto-retry
- `ORDER_ALREADY_COMPLETED` (422)
- `AMOUNT_MISMATCH` (422) — captured ≠ authorized amount
- `RATE_LIMIT_REACHED` (429) — exponential backoff; honor `Retry-After`
- `INTERNAL_SERVER_ERROR` (500) — retry with same `PayPal-Request-Id`; log `debug_id`
- `PLAN_STATUS_INVALID` — plan must be `ACTIVE`
- `SUBSCRIPTION_STATUS_INVALID` — state disallows this operation

Always include the full error response body and `debug_id` when contacting PayPal support at https://developer.paypal.com/support/.
