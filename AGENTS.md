# AGENTS.md — The Financial Detective, with Leash (PayPal AI Hackathon)

Operating manual for AI agents working in this repo. Keep it in sync with the source-of-truth plan: **Rev 2.3 (Oct 25, 2026)**. The plan lives outside the repo (`~/projects/paypalContractor-plan/`); cite it by section number (e.g. §7.5, A.4) rather than guessing.

## 0. Repo reality
- **Initialized public repo:** `https://github.com/Ahmed-fall/PaypalContractor.git`, trunk-based on protected `main`.
- **Product:** *The Financial Detective* — a B2B contract-to-payment compliance auditor + remediation engine. Upload a signed contract → AI compiles it to a strict JSON rule schema → live PayPal captures are audited → breaches produce a real PayPal supplemental invoice + QR code.
- **Leash** is the built-in safety and payroll layer: one Allow/Ask/Deny guard in front of every outgoing money action, plus milestone payroll paid through PayPal Payouts.
- **Five-person team.** The human in this session is **P4 · Ahmed** (Integration, QA, Security & Submission). Ownership boundaries are strict (§3).
- **Superseded:** Revision 1 (4-person, Detective only) and Revs 2.1/2.2. Do not implement retired ideas (shopping agent, MCP at runtime, mandates, vault/authorize-void, `LATE_BREACH`).

## 1. Read the plan first
Rev 2.3 is the locked plan (18 sections + Appendix A + 5 assignment sheets). If a `.docx` copy is available, extract it with `python-docx`:

```bash
python3 -c "import docx; print('\n'.join(p.text for p in docx.Document('$HOME/projects/paypalContractor-plan/Paypal Financial Detective.docx').paragraphs))"
```

Section map: §2 evidence · §3 compliance matrix · §4 PayPal dossier (SRC/PIT tables) · §5 product spec (5.1 Detective, 5.2 guard, 5.3 payroll) · §6 requirements · §7 **architecture + interfaces (7.5) + dependency freeze (7.6)** · §8 ownership map · §9–11 work packages/critical path/milestones · §12 collaboration · §13 security · §14 tests + gates · §15 judging · §16 video/submission · §17 audit · §18 open questions · **Appendix A** (A.1 deployment/env, A.2 API examples incl. A.2.1 Detective, A.2.2 Leash, A.2.3 Payouts, A.3 DB schema, A.4 errors, A.5 state machines, A.6 demo key, A.7 pages, A.8 logging).

## 2. Deadline & timezone (non-negotiable)
- **Official deadline:** Thu **Nov 12 2026, 12:00 PST = 22:00 Africa/Cairo** (Egypt is on EET, UTC+2).
- **Internal freeze:** **Nov 11 2026 22:00 Cairo** — zero new code; emergency hotfixes only.
- **Feature freeze / MVP lock:** **Nov 5**. Demo video target **2:50**, hard cap **< 3:00** (REQ-COMP-03: 2:15–2:50). Leash segment ≤ 70 s.
- **Leash checkpoints:** **L1 Oct 27** (2-item Payouts batch `SUCCESS`, duplicate `sender_batch_id` rejected), **L2 Nov 3** (full money-in + money-out flow on Render).

## 3. Ownership boundaries (Rev 2.3 §8.2) — never cross them
| Owner | Exclusive write scope | Must NOT touch |
| --- | --- | --- |
| **P1 · Hassan** Backend/PayPal | `backend/{main.py,config.py,db/,api/}`, `services/paypal/*` (except `payouts*.py`), `tests/unit_paypal/`, `requirements.txt` | `ai/`, `leash/`, `api/leash.py`, React, `schemas/contracts.py` |
| **P2 · Yassin** AI | `backend/app/ai/**` incl. `ai/leash/`, `tests/unit_ai/` | PayPal HTTP calls, SQLAlchemy, routes, React, schema edits |
| **P3 · Moamen** Frontend/AG Grid | `frontend/**` except `src/types/` and `components/Leash/`, `__tests__/` (non-Leash) | backend Python, browser PayPal secrets, `types/contracts.ts` |
| **P4 · Ahmed** (this session) Integration/QA/Sec | `backend/app/schemas/contracts.py`, `frontend/src/types/contracts.ts`, `shared/**` (except `shared/fixtures/leash/`), `backend/tests/integration/**`, `LICENSE`, `README`, `AGENTS.md`, `docs/`, `.env.example`, `.gitignore`, `.github/**`, `docker-compose.yml`, Dockerfiles, `render.yaml`, `scripts/` (except `scripts/payouts_l1.py`), video, Devpost | feature code in P1/P2/P3/P5 dirs |
| **P5 · Nour** Leash | `backend/app/leash/**`, `api/leash.py`, `services/paypal/payouts.py` + `payouts_sync.py`, `tests/unit_leash/**`, `shared/fixtures/leash/**`, `scripts/payouts_l1.py`, `frontend/src/components/Leash/**` | prompts (`ai/`), P1 routers/services except hook H2 via PR, P3 components, canonical schemas (open a P4 PR) |

- Canonical contract changes require a **P4-approved PR** plus every affected consumer (`schemas/contracts.py`, `types/contracts.ts`).
- Review couplings: `payouts*.py` need **P1** review; `components/Leash/` need **P3** review; `ai/leash/` signature changes need **P5** review; `api/leash.py` is mounted by **P1**.
- **AI public surface** (§7.5 Interface 3): `extract_contract_rules(...)`, `evaluate_payment_compliance(...)` (P2 → P1); `compile_leash_policy(...)`, `map_timesheet_columns(...)`, `explain_decision(..., use_llm)` (P2 → P5); `evaluate_rules(...)`, `decide(...)`, `on_milestone_payment(...)`, `lines_ready_to_send(...)`, `send_payroll_batch(...)`, `apply_item_status(...)`, `recompute_run_status(...)` (P5 → P1/P5).

## 4. Canonical interfaces & mock-first workflow
- Frozen interfaces: `backend/app/schemas/contracts.py` (Pydantic) + `frontend/src/types/contracts.ts` (TS mirror). Detective types frozen in WP-4.1; **Leash types + Interface 3 functions frozen Oct 24** (P5 PR, P4-approved). Treat both as the executable source of truth; Appendix A.2 examples are the wire format.
- **Reason vs lifecycle:** `DiscrepancyType` (immutable audit reason: `COMPLIANT, UNDERPAID, LATE_PAYMENT, UNDERPAID_AND_LATE, CURRENCY_MISMATCH`) is **separate** from `DiscrepancyStatus` (`NONE, PENDING_APPROVAL, BLOCKED, REMEDIATED`). Remediation changes status only; it never overwrites the type. DB lifecycle has more states (`OPEN`, `SEND_FAILED`, `RECOVERED`, `DISMISSED`) — API enum stays the four above.
- **Milestone IDs:** AI returns `ExtractedMilestoneSpec` (no DB id); backend returns `PersistedMilestoneSpec` (`milestone_id`), assigned on `/extract` and preserved on `/activate`.
- **Money is a string** with 2 decimals (`"850.00"`) in every JSON body; `Decimal` in Python; never a JSON number.
- **Key endpoints** (§7.5 Interface 4): `POST /contracts/extract`, `POST /contracts/{id}/activate` (never `/confirm`), `POST /simulator/pay`, `GET /ledger`, `POST /discrepancies/{id}/remediate`, `POST /webhooks/paypal`, `GET/POST /leash/policy*`, `GET /leash/decisions`, `GET/POST/PATCH /leash/team`, `POST /leash/timesheets` + `/{id}/mapping`, `POST/GET /leash/payroll/runs*`, `PATCH …/lines/{line_id}`, `POST …/send`, `GET /leash/ledger/margin`, `GET /health`, `POST /demo/reset`.
- **Every write needs `X-Demo-Key`** (A.6) except `/webhooks/paypal`. Reads are open. The key never ships in the frontend bundle; judges get it via Devpost/README.
- Canonical fixtures: `shared/fixtures/contracts/{Acme_Standard.pdf, Globex_LateFee.pdf, Initech_MultiMilestone.pdf}`; Detective JSON in `shared/fixtures/json/`; Leash fixtures (P5) in `shared/fixtures/leash/`. AI signature: `extract_contract_rules(file_bytes: bytes, filename: str)`.
- Frontend builds against fixtures with `VITE_USE_MOCKS=true` (400 ms latency) until the Nov 2–5 switchover (`VITE_USE_MOCKS=false`).

## 5. Commands
```bash
# Backend (Python 3.11; ruff + mypy strict on schemas/ai/leash)
ruff check . && mypy app
pytest backend/tests/unit_paypal    # P1
pytest backend/tests/unit_ai        # P2
pytest backend/tests/unit_leash     # P5
pytest backend/tests/integration    # P4 E2E / release gates
# Frontend (zero `any` in types/contracts.ts and api/client.ts)
npm run build && npx tsc --noEmit && npx eslint .
# Full stack + secrets
docker-compose up --build           # frontend :5173, backend :8000/api/v1/health
gitleaks detect --source . -v
```
CI order on every PR: **gitleaks → ruff → mypy → pytest (`unit_paypal`, `unit_ai`, `unit_leash`, `integration`) → tsc**.

## 6. PayPal pitfalls (plan §4.5, Table PIT-01…PIT-11)
- **PIT-01:** citation/breakdown goes in `detail.note` + `items[].description` at invoice **draft creation**; `/send` ignores its own `subject`/`note`.
- **PIT-02:** never use Transaction Search for the live demo (≤ 3 h indexing). Use Orders v2 + webhooks; reporting is backfill only.
- **PIT-03:** sandbox webhooks lag 10–60 s **and** can race `/simulator/pay`. Commit `paypal_captures` first in its own transaction (`ON CONFLICT (paypal_capture_id) DO NOTHING`) before evaluation/hooks; the winner runs the pipeline and sets `processed_at`; the loser only sets `webhook_verified = true`. If a claim is stuck (> 30 s, `processed_at` empty), re-claim atomically and re-run (crash recovery, R2).
- **PIT-04:** standardize JS SDK v6 (`createInstance({clientToken})`, 15-min token) or pin v5 — never mix.
- **PIT-06:** UNIQUE `paypal_capture_id` / `paypal_invoice_id` + deterministic `PayPal-Request-Id` (`uuid5(NAMESPACE_DNS, f"{discrepancy_id}:remediate")`; payouts `f"{run_id}:payout"`).
- **PIT-08:** `sender_batch_id = fd-<milestone>-<run_id>` is the double-pay guard; PayPal rejects reuse within 30 days. A demo reset uses new run IDs.
- **PIT-09:** sandbox has **no `PHONE` recipients** — pay team members by `EMAIL` only.
- **PIT-10:** a Payouts `201` means `PENDING`, not paid; batches cannot be voided. Approval happens before sending; poll every 10 s for 2 min.
- **PIT-11:** Invoicing rejects a repeated `invoice_number` (422 `DUPLICATE_INVOICE_NUMBER`). Persist `remediation_invoices` as `DRAFT` right after draft creation; retry from `SEND_FAILED` resumes at `/send` only if amount+recipient are unchanged, else delete the draft, re-run `decide()` and create `-R<attempt>` (unique on `(discrepancy_id, attempt)`).
- **Webhooks:** persist `webhook_events` with UNIQUE PayPal event id (`WH-…`); short-circuit repeats with 200; dispatch `PAYMENT.PAYOUTS*` to P5's `payouts_sync.py`. Cache the `PAYPAL-CERT-URL` X.509 PEM in memory (TTL) and require the hostname to end with `.paypal.com` (SSRF guard).
- **OAuth:** one process-wide token cache; refresh when TTL < 300 s or on HTTP 401. Startup refuses any `PAYPAL_API_BASE_URL` other than `https://api-m.sandbox.paypal.com` (exact match).

## 7. PayPal AI developer tools (this repo)
Three different things — do not conflate:
- **`paypal/AI-Toolkit`** = **IDE coding-agent plugin** (Claude Code / Cursor / Codex; also binds an MCP server). Not a runtime SDK — never import it into the app.
- **`@paypal/agent-toolkit`** = runtime function-calling SDK (`create_invoice, send_invoice, send_invoice_reminder, create_order, list_transactions`, …).
- **`@paypal/mcp` / `mcp.sandbox.paypal.com`** = MCP server for live sandbox operations.

opencode has **no plugin-marketplace support**, so this repo wires the Toolkit's parts natively:
- **MCP:** `opencode.json` (git-ignored) defines a local `paypal` server via `npx -y @paypal/mcp --tools=all`, `PAYPAL_ENVIRONMENT=SANDBOX`, token read from `~/.config/opencode/paypal_token` (≈28 tools). Sandbox endpoint if remote is used: `https://mcp.sandbox.paypal.com/sse` (production `mcp.paypal.com` — **never** here).
- **Skill:** `opencode.json` → `skills.paths` → `/home/ahmed/tools/paypal-ai-toolkit/skills` (`paypal-best-practices`). Update: `git -C /home/ahmed/tools/paypal-ai-toolkit pull`.
- **Commands:** `/paypal-doctor`, `/paypal-explain-error`, `/paypal-sandbox`, `/paypal-setup`, `/paypal-test-accounts` in `.opencode/command/`.
- **Hook gap:** the Toolkit's `PreToolUse` hook has no opencode equivalent; the checklist is enforced via the skill + this file.

**Token & restart gotchas:**
- Sandbox access tokens expire in ≤ 8 h. Refresh: `curl -sX POST https://api-m.sandbox.paypal.com/v1/oauth2/token -u "$CLIENT_ID:$SECRET" -d grant_type=client_credentials | jq -r .access_token | tr -d '\n' > ~/.config/opencode/paypal_token` (use `/paypal-setup refresh`).
- The token file must be a **single line, no trailing newline**.
- Config is **not hot-reloaded**; quit and restart opencode after changing `opencode.json`, skills, or commands.
- Never put the AI-Toolkit/MCP on the application's runtime critical path; the app manages its own OAuth2 via `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`.

## 8. Financial-security invariants (plan §13)
- **Authoritative state only** from `POST /v2/checkout/orders/{id}/capture` returning `COMPLETED`, `verify-webhook-signature` returning `SUCCESS`, or a Payouts item status `SUCCESS`. Never from the browser, LLM, or polling optimism.
- **Decimal only:** quantize to `0.01` with `ROUND_HALF_UP`, store `NUMERIC(12,2)`; floats banned in financial paths (both Detective and Leash).
- **AI never computes money or decisions:** `compliance_math.py` and `leash/payroll/calculate.py` compute amounts; `leash/guard.py` computes Allow/Ask/Deny. The LLM extracts, compiles rules for confirmation, maps timesheet columns, and writes prose (`reason` ≤ 200 chars). Hard rule: **AI can raise suspicion; only code can say yes.**
- **Prompt injection:** wrap payer notes, contract text and timesheet cells as passive `<untrusted_input>` data; decisions never read AI output.
- **Human-in-the-loop, policy-driven:** correction invoices auto-send only when the owner-confirmed policy says `ALLOW` (default G-01 ≤ $250, confidence ≥ 0.90, clause quoted); otherwise `confirm_send: true`. **Payroll batches are never auto-sent** — one owner approval per batch, and `POST …/send` returns **409** while any Ask is unresolved.
- **Fail closed:** invoice guard uses templates only (< 5 ms); payroll reasons use a background task with a 3 s LLM budget + template fallback; an LLM error never changes a decision; a failed rule compile leaves the previous policy active.
- **No prod bypass:** `/webhooks/paypal` has zero signature-bypass flags in every environment; webhook tests use mocked transports (respx/httpx), not bypass switches. Writes require `X-Demo-Key` (hmac.compare_digest).
- **Secrets:** `.env` is git-ignored; only `.env.example` placeholders are committed; gitleaks in CI; on leak, rotate and purge history with `git filter-repo`.

## 9. Collaboration & release gates (plan §12, §14.2)
- Trunk-based on protected `main`; no direct pushes. Branches `feat/p1-…`, `feat/p2-…`, `feat/p5-…`, `chore/p4-…`, `fix/…`. Conventional Commits. 1 peer review (≤ 6 h SLA).
- **Six gates block recording/submitting:** (1) zero mocked PayPal in the prod path, (2) math + idempotency tests (TST-05/06/09/10), (3) zero leaked secrets, (4) root Apache-2.0 LICENSE + clean `docker-compose up --build`, (5) live Render HTTPS health (TST-12), (6) **Leash safety** (TST-13/14/15/20/21/22/24). Tests TST-01…TST-24; audit AUD-01…AUD-07.
- **Milestones:** M1 Oct 17–19 (done) · M2 Oct 20–25 · **L1 Oct 27** · M3 Oct 26–Nov 1 · **L2 Nov 3** · M4 Nov 2–5 (MVP lock) · M5 Nov 6–8 · M6 Nov 9–11 · freeze Nov 11 22:00 · protected buffer to Nov 12 22:00.
- Rollback: tags `v0.1-m1`, `v0.2-m2`, `v0.3-l2`, `v1.0-mvp`; revert a breaking merge and roll Render back.

## 10. Demo fallbacks (plan §11)
- **A** webhook delayed/racing → synchronous capture runs the pipeline; webhook converges; stuck claims re-claimed after 30 s.
- **B** `/generate-qr-code` 500 → build a local QR from the invoice's `payer-view` link (P3).
- **C** LLM outage → Gemini 2.5 Flash → GPT-4o-mini → deterministic template (never crash); Leash invoice guard uses templates only.
- **D** Payouts slow → poll 2 min, show `PENDING` honestly; record with a pre-proven batch.
- **E** Leash behind → cut compiler→plain form, XLSX→CSV, margin columns, monthly mode. **Never cut** PR-01, PR-06, the 409 guard, or hook H1.
- **F** Payouts blocked → keep the guard + auto-send; show the prepared run as "ready for Payouts approval"; decide at L1.

## 11. Submission artifacts (plan §16–17)
Public repo + root `LICENSE` (Apache-2.0, visible in "About") · all source + `shared/fixtures/` (3 contracts + Leash roster/timesheets) · `.env.example` · README (hosted URL + `docker-compose up --build` + Leash section) · live Render HTTPS demo · **public** YouTube video 2:15–2:50 (cap < 3:00) · Devpost listing PayPal APIs (Orders v2, Webhooks v1, Invoicing v2, **Payouts v1**), AI models (Gemini 2.5 Flash, GPT-4o-mini), and AG Grid/Render opt-ins.

## 12. Resolved errata & Rev 2.3 deltas
1. **Reason/lifecycle split** — orthogonal enums; `LATE_PAYMENT` (no `LATE_BREACH`); ledger exposes both.
2. **Endpoint** — `POST /contracts/{id}/activate` (`/confirm` retired).
3. **AI signature** — `async extract_contract_rules(file_bytes: bytes, filename: str) -> ContractExtractionResult` (extension + `%PDF-` sniffing).
4. **Milestone IDs** — `ExtractedMilestoneSpec` (AI) vs `PersistedMilestoneSpec` (adds `milestone_id`); assigned on `/extract`, preserved on `/activate`.
5. **Canonical fixtures** — `Acme_Standard.pdf`, `Globex_LateFee.pdf`, `Initech_MultiMilestone.pdf` (locked).
6. **Gates** — deduplicated to six (see §9).
7. **Schedule** — M1–M6 + L1/L2; freeze Nov 11 22:00 Cairo; deadline Nov 12 22:00 Cairo.
8. **No dev webhook bypass** — Rev 2.3 defines no `/dev/*` route; unit tests mock PayPal transports; the real webhook path is signature-only.
9. **Webhook hardening** — `webhook_events` UNIQUE event id; `paypal_captures` UNIQUE `paypal_capture_id` with `claimed_at`/`processed_at` crash recovery; X.509 cert caching + `*.paypal.com` SSRF guard.
10. **Post-MVP** — reminders (`/discrepancies/{id}/remind`), Transaction Search backfill, expanded monthly payroll UI (Rev 2.3 §5.1 "Expansion").
11. **Currency mismatch** — `captured_currency` (optional ISO-4217, `^[A-Z]{3}$`) on `LedgerRow` and `DiscrepancyEvaluationResult`; contract-currency amounts stay single-currency; foreign captures excluded from `total_captured`; no FX; remediation policy is P2's.
12. **Rev 2.3 (Oct 25)** — Leash guard + payroll are in scope: `DiscrepancyStatus.BLOCKED` added; `LedgerRow.leash_decision`/`remediation_sent_by`; `SimulatorPaymentResponse` carries `capture_status`, `discrepancy_id`, `leash`, `remediation_invoice`, `payroll_run_id`; `HealthResponse` exposes `leash_enabled`/`ai_provider`; Leash Interface 2 types frozen Oct 24 (P5 PR, approved by P4); cents-based margin totals (R4); template-vs-LLM explanation split (R5); capture-lock crash recovery (R2); safe draft resume (R3).
