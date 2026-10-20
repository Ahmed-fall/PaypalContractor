---
description: Show PayPal sandbox test accounts and scenario-specific testing tips
agent: build
---

If "$ARGUMENTS" is set, show only the relevant section(s) plus Key Gotchas. If empty, show all.

## Sandbox Buyer & Seller Accounts
Create at https://developer.paypal.com/dashboard/accounts:
- **Personal (Buyer)** — approves payments, Venmo, Pay Later, opens disputes
- **Business (Seller/Merchant)** — receives payments, creates orders, invoices, subscriptions

Sandbox base URL: `https://api-m.sandbox.paypal.com`

## Quick Access Token (Sandbox)
```bash
curl -X POST https://api-m.sandbox.paypal.com/v1/oauth2/token \
  -u "$PAYPAL_CLIENT_ID:$PAYPAL_CLIENT_SECRET" \
  -d "grant_type=client_credentials"
```

## Test Scenarios
- **Successful payment** — any buyer with sufficient balance.
- **Declined** — buyer card set to "Declined"; insufficient funds; or a risk-triggering amount (`TRANSACTION_REFUSED`).
- **BNPL / Pay Later** — $30–$1,500 (Pay in 4) or $199–$10,000 (Pay Monthly). Sandbox tests only the initial capture.
- **Venmo** — select "Venmo" in the funding picker; confirm `payment_source.venmo` in the capture response.
- **Subscriptions** — product → ACTIVE plan → subscription; sandbox auto-processes renewals; use the Webhooks Simulator for `BILLING.SUBSCRIPTION.PAYMENT.FAILED`.
- **Fastlane** — sandbox buyer emails must contain `whitelistallblock` (e.g. `test-fl@whitelistallblock.com`). Test cards: Visa `4005 5192 0000 0004`, `4012 0000 3333 0026`, `4012 0000 7777 7777`; Mastercard `5555 5555 5555 4444`; Amex `3782 822463 10005`.
- **Disputes** — pay as buyer, log in at https://sandbox.paypal.com → Report a problem; `GET /v2/customer/disputes`.
- **Webhooks** — expose a public HTTPS URL (PayPal cannot deliver to localhost); register via `POST /v1/notifications/webhooks`; use the Webhooks Simulator for instant events.

## Key Gotchas
- Sandbox and production credentials are completely separate — never mix.
- Sandbox tokens only work against `api-m.sandbox.paypal.com`.
- Buyer approval requires https://sandbox.paypal.com (not the live site).
- Sandbox webhook events may be delayed; use the Webhooks Simulator for immediate testing.
