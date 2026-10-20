# AGENTS.md — The Financial Detective (PayPal AI Hackathon)

Operating manual for AI agents working in this repo. Keep it in sync with the source-of-truth plan. Exhaustive detail lives in `~/projects/paypalContractor-plan/Paypal Financial Detective.docx` (kept **outside** this repo so it can never be committed); cite it by section/table number rather than guessing.

## 0. Repo reality
- **Greenfield project.** The authoritative plan (18 sections + 15 tables) lives at `~/projects/paypalContractor-plan/Paypal Financial Detective.docx`, outside the repo. No application code, no git repo yet. The human will create the public GitHub repo later; do not `git init` unless asked.
- **Product:** *The Financial Detective* — a B2B contract-to-payment compliance auditor + remediation engine. Upload a signed contract → AI compiles it to a strict JSON rule schema → live PayPal captures/invoices are audited → breaches produce a human-approved **real PayPal supplemental invoice** + QR code.
- **Built by a 4-person team.** The human in this session is **Person 4** (Integration, QA, Security & Submission Lead) and will use opencode to review teammates' work. Ownership boundaries are strict (see §3).

## 1. Read the plan first
The `.docx` is **not plain text** — the `Read` tool cannot parse it. Extract it with `python-docx` (installed):

```bash
# Paragraphs
python3 -c "import docx; print('\n'.join(p.text for p in docx.Document('$HOME/projects/paypalContractor-plan/Paypal Financial Detective.docx').paragraphs))"
# Tables (all 15 matrices)
python3 -c "import docx; d=docx.Document('$HOME/projects/paypalContractor-plan/Paypal Financial Detective.docx'); [print('TABLE',i) or [print(' || '.join(c.text.strip() for c in r.cells)) for r in t.rows] for i,t in enumerate(d.tables)]"
```

Section map: §2 evidence register · §3 compliance matrix · §4 **PayPal AI Toolkit dossier** · §5 product spec · §6 engineering reqs · §7 **architecture + interface contracts** · §8 ownership map · §9–11 work packages/critical path/milestones · §12 collaboration standards · §13 security · §14 tests + release gates · §15 judging/prizes · §16 demo + submission · §17 audit · plus 4 assignment sheets (P1–P4).

## 2. Deadline & timezone (non-negotiable)
- **Official deadline:** Thu **Nov 12 2026, 12:00 PST = 22:00 Africa/Cairo (EET)**.
- **Internal freeze:** **Nov 11 2026 22:00 Cairo** — zero new code; emergency hotfixes only.
- **Feature freeze:** Nov 5. Demo video target **2:40**, hard cap **< 3:00**. README/Devpost must be complete before the internal freeze.

## 3. Ownership boundaries — never cross them
| Owner | Exclusive write scope | Must NOT touch |
| --- | --- | --- |
| **P1** Backend/PayPal | `backend/{main.py,config.py,db/,api/,services/paypal/}`, `backend/tests/unit_paypal/`, `requirements.txt` | `backend/app/ai/`, frontend, `schemas/contracts.py` |
| **P2** AI | `backend/app/ai/**`, `backend/tests/unit_ai/**` | PayPal HTTP calls, SQLAlchemy, frontend, `contracts.py` |
| **P3** Frontend/AG Grid | `frontend/src/**` (except `types/`), `package.json`, configs, `__tests__/` | backend Python, PayPal secrets in browser, `types/contracts.ts` |
| **P4** (this session) Integration/QA/Sec | `backend/app/schemas/contracts.py`, `frontend/src/types/contracts.ts`, `shared/**`, `backend/tests/integration/**`, `LICENSE`, `README`, `.env.example`, `.gitignore`, `.github/**`, `docker-compose.yml`, Dockerfiles, `render.yaml`, `scripts/`, docs, video | feature code in P1/P2/P3 dirs |

Editing the two canonical contract files requires a P4-approved PR plus the affected consumers. AI public surface: `extract_contract_rules(...)` and `evaluate_payment_compliance(...)` in `backend/app/ai/__init__.py`.

## 4. Canonical interfaces & mock-first workflow
- Frozen interfaces (P4, due Day 3): `backend/app/schemas/contracts.py` (Pydantic) and `frontend/src/types/contracts.ts` (TS mirror), plus deterministic JSON fixtures in `shared/fixtures/json/`. These two files already encode the reviewed errata (see §12) — treat them as the executable source of truth.
- **Reason vs lifecycle:** `DiscrepancyType` (immutable audit reason: `COMPLIANT, UNDERPAID, LATE_PAYMENT, UNDERPAID_AND_LATE, CURRENCY_MISMATCH`) is **separate** from `DiscrepancyStatus` (`NONE, PENDING_APPROVAL, REMEDIATED`). Remediation changes status only; it never overwrites the type. (`LATE_BREACH` is retired — use `LATE_PAYMENT`.)
- **Milestone IDs:** the AI extractor returns `ExtractedMilestoneSpec` (has `milestone_code`, no DB id); the backend returns `PersistedMilestoneSpec` (adds `milestone_id`). IDs are assigned on `/extract` and preserved on `/activate`.
- Key REST endpoints: `POST /api/v1/contracts/extract`, `POST /api/v1/contracts/{id}/activate` (never `/confirm`), `POST /api/v1/simulator/pay`, `GET /api/v1/ledger`, `POST /api/v1/discrepancies/{id}/remediate`, `POST /api/v1/webhooks/paypal`. Post-MVP: `POST /api/v1/discrepancies/{id}/remind` (REQ-EXT-01). Test-only: `POST /api/v1/dev/simulate-webhook` (mounted only when `ENV == "local_test"`).
- Canonical fixture set: `shared/fixtures/contracts/{Acme_Standard.pdf, Globex_LateFee.pdf, Initech_MultiMilestone.pdf}`. AI signature is `extract_contract_rules(file_bytes: bytes, filename: str)`.
- Frontend builds against fixtures with `VITE_USE_MOCKS=true` (400 ms simulated latency) — no backend dependency until the Day 17 switchover.

## 5. Commands
```bash
# Backend (Python 3.11, ruff + mypy strict on schemas/ai)
ruff check . && ruff format --check . && mypy app
pytest backend/tests/unit_paypal    # P1
pytest backend/tests/unit_ai        # P2
pytest backend/tests/integration    # P4 E2E / release gates
# Frontend (zero `any` in types/contracts.ts and api/client.ts)
npm run build && npx tsc --noEmit && npx eslint .
# Full stack + secrets
docker-compose up --build           # frontend :5173, backend :8000/api/v1/health
gitleaks detect --source . -v
```
CI order on every PR: **gitleaks → ruff → mypy → pytest → tsc**.

## 6. PayPal API pitfalls (verified — see docx §4.5, Table 3)
- **PIT-01:** the AI breach citation/breakdown must go in `detail.note` + `items[].description` at invoice **draft creation**. The `/send` body's `subject`/`note` are **silently ignored**.
- **PIT-02:** never use `GET /v1/reporting/transactions` for the live demo (up to 3 h indexing delay). Use Orders v2 + webhooks; reporting is historical backfill only.
- **PIT-03:** sandbox webhooks can lag 10–60 s. Trigger the audit synchronously on `/capture` return and idempotently upsert when the webhook arrives.
- **PIT-04:** standardize JS SDK **v6** (`web-sdk/v6/core`, `createInstance({clientToken})`, 15-min token) or pinned v5 — never mix.
- **PIT-06:** UNIQUE constraint on `paypal_capture_id`/`paypal_invoice_id` + deterministic `PayPal-Request-Id` (`uuid5(NAMESPACE_DNS, f"{discrepancy_id}:remediate")`) to prevent duplicate invoices.
- **Webhook events:** persist `paypal_webhook_events` with UNIQUE `event_id` (PayPal `WH-…`) and short-circuit repeats with 200; upsert `paypal_captures` on `paypal_capture_id` so sync capture and webhook converge. Cache the `PAYPAL-CERT-URL` X.509 PEM in memory (TTL) and require the hostname to end with `.paypal.com` (SSRF guard) before fetching.
- OAuth: cache the bearer token in memory, refresh when TTL < 300 s or on HTTP 401. Startup guardrail: assert `"sandbox.paypal.com"` is in `PAYPAL_API_BASE_URL`.

## 7. PayPal AI developer tools (this repo)
Three different things — do not conflate:
- **`paypal/AI-Toolkit`** = **IDE coding-agent plugin** (Claude Code / Cursor / Codex; also binds an MCP server). It is **not** a runtime SDK — never import it into the app.
- **`@paypal/agent-toolkit`** = runtime function-calling SDK (`create_invoice, send_invoice, send_invoice_reminder, create_order, list_transactions`, …).
- **`@paypal/mcp` / `mcp.sandbox.paypal.com`** = MCP server for live sandbox operations.

opencode has **no plugin-marketplace support**, so this repo wires the Toolkit's parts natively:
- **MCP:** `opencode.json` defines a local server `paypal` that launches `npx -y @paypal/mcp --tools=all` with `PAYPAL_ENVIRONMENT=SANDBOX` and the token read at startup from `~/.config/opencode/paypal_token` (≈28 tools: `create_invoice`, `send_invoice`, `send_invoice_reminder`, `generate_invoice_qr_code`, `create_order`, `list_transactions`, …). Sandbox endpoint if remote is used: `https://mcp.sandbox.paypal.com/sse` (production `mcp.paypal.com` — **never** use it here).
- **Skill:** `opencode.json` → `skills.paths` points at the clone `/home/ahmed/tools/paypal-ai-toolkit/skills` (`paypal-best-practices` + `references/`). To update: `git -C /home/ahmed/tools/paypal-ai-toolkit pull`.
- **Commands:** `/paypal-doctor`, `/paypal-explain-error`, `/paypal-sandbox`, `/paypal-setup`, `/paypal-test-accounts` in `.opencode/command/`.
- **Hook gap:** the Toolkit's `PreToolUse` best-practices hook has no opencode equivalent; the checklist is enforced via the `paypal-best-practices` skill + this file instead.

**Token & restart gotchas:**
- Sandbox access tokens expire in ≤ 8 h. Refresh: `curl -sX POST https://api-m.sandbox.paypal.com/v1/oauth2/token -u "$CLIENT_ID:$SECRET" -d grant_type=client_credentials | jq -r .access_token | tr -d '\n' > ~/.config/opencode/paypal_token` (use `/paypal-setup refresh`).
- The token file must be a **single line, no trailing newline** — whitespace breaks the `Authorization` header.
- Config is **not hot-reloaded**; after changing `opencode.json`, skill, or commands, **quit and restart opencode**.
- Never put the AI-Toolkit/MCP on the application's runtime critical path; the app manages its own OAuth2 via `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`.

## 8. Financial-security invariants (docx §7.3, §13)
- **Authoritative state only** from `POST /v2/checkout/orders/{id}/capture` returning `COMPLETED`, or `POST /v1/notifications/verify-webhook-signature` returning `SUCCESS`. Never trust the browser or LLM output.
- **Decimal only:** quantize to `0.01` with `ROUND_HALF_UP`, store `NUMERIC(12,2)`. Floats are banned in financial paths.
- **AI never computes money:** `compliance_math.py` owns `shortfall_amount`, `penalty_amount`, `total_remediation_amount`; the LLM only extracts params and writes prose (clause citation, `detail.note`).
- **Prompt injection:** wrap payer notes/contract text as passive `<untrusted_*>` data; compliance status is deterministic.
- **Human-in-the-loop:** discrepancies stay `PENDING_APPROVAL` until an explicit `confirm_send: true` remediation call.
- **No prod bypass:** `/api/v1/webhooks/paypal` has zero signature-bypass flags in every environment. Synthetic webhook testing uses `/api/v1/dev/simulate-webhook`, mounted only when `ENV == "local_test"`; `config.py` raises at startup if dev routes are enabled anywhere else.
- **Secrets:** `.env` is git-ignored; only `.env.example` placeholders are committed; gitleaks in CI; on leak, rotate and purge history with `git filter-repo`.

## 9. Collaboration & release gates (docx §12, §14.2)
- Trunk-based on protected `main`; no direct pushes. Branches `feat/p1-…`, `fix/p2-…`. Conventional Commits. 1 peer review (≤ 6 h SLA).
- The 5 gates block recording/submitting: (1) zero mocked PayPal in the prod path, (2) math + idempotency tests pass, (3) **zero leaked secrets + dev routes disabled on Render**, (4) root Apache-2.0 LICENSE + clean `docker-compose up --build`, (5) live Render HTTPS health. Tests are TST-01…TST-12; audit checks AUD-01…AUD-06.
- Schedule: Days 1–25 (Oct 17 – Nov 10) active critical path; **Day 26 = Wed Nov 11 22:00 Cairo** hard internal submission freeze; Day 27 = protected 24 h buffer.

## 10. Demo fallbacks (docx §11)
- **A** webhook delay → synchronous `/capture` triggers the audit; webhook upserts idempotently.
- **B** `/generate-qr-code` 500 in sandbox → build a local QR from the invoice's `payer-view` link.
- **C** LLM outage → Gemini 2.5 Flash → GPT-4o-mini → deterministic template fallback (never crash the demo).

## 11. Submission artifacts (docx §16–17)
Public repo + root `LICENSE` (Apache-2.0, visible in "About") · all source + `shared/fixtures/` (3 sample PDFs) · `.env.example` · README (hosted URL + `docker-compose up --build`) · live Render HTTPS demo · **public** YouTube video < 2:50 · Devpost fields listing PayPal APIs used, AI models, and AG Grid/Render prize opt-ins.

## 12. Resolved errata (adopted — the docx is being updated by the owner)
1. **Reason/lifecycle split:** `DiscrepancyType` and `DiscrepancyStatus` are orthogonal enums across DB/API/UI; `LATE_BREACH` → `LATE_PAYMENT`; ledger exposes both fields.
2. **Endpoint:** standardized on `POST /api/v1/contracts/{id}/activate` (REQ-FE-01 `/confirm` is retired).
3. **AI signature:** `async extract_contract_rules(file_bytes: bytes, filename: str) -> ContractExtractionResult` (extension + `%PDF-` magic-byte sniffing).
4. **Milestone IDs:** `ExtractedMilestoneSpec` (AI) vs `PersistedMilestoneSpec` (adds `milestone_id`); assigned on `/extract`, preserved on `/activate`.
5. **Canonical fixtures:** `Acme_Standard.pdf`, `Globex_LateFee.pdf`, `Initech_MultiMilestone.pdf` (locked everywhere).
6. **§14.2 gates:** deduplicated to Gates 1–5.
7. **Schedule:** Days 1–25 active; Day 26 = Nov 11 22:00 Cairo freeze; Day 27 = protected buffer (see §9).
8. **Dev-route isolation:** `POST /api/v1/dev/simulate-webhook` only when `ENV == "local_test"`; no bypass on the real webhook route.
9. **Webhook hardening:** `paypal_webhook_events.event_id` UNIQUE + `paypal_captures.paypal_capture_id` UNIQUE; X.509 cert caching + `*.paypal.com` SSRF guard.
10. **Post-MVP:** `POST /api/v1/discrepancies/{id}/remind` (REQ-EXT-01) wrapping Invoicing v2 `/remind`.
11. **Currency mismatch representation:** `LedgerRow` and `DiscrepancyEvaluationResult` carry `captured_currency` (optional ISO-4217, `^[A-Z]{3}$`) so `CURRENCY_MISMATCH` is auditable; contract-currency amounts (`expected_amount`, `shortfall_amount`, `penalty_amount`, `total_remediation_amount`) stay single-currency and foreign captures are excluded from `total_captured` — no FX conversion. The remediation *policy* for a mismatch (flag-only vs re-invoice the full milestone) lives in `compliance_math.py` and is P2's decision.
