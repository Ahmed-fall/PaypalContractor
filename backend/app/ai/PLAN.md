# Person 2 — AI & Compliance Engine: Work Plan

Owner: **P2**. Scope: `backend/app/ai/**` and `backend/tests/unit_ai/**` only.
Source of truth: the project plan (P2 assignment sheet, REQ-AI-01/02, TST-03..07), `AGENTS.md`, and `shared/fixtures/`.

## Goal
A stateless, self-contained Python package that P1 imports to:
1. Turn an uploaded contract (PDF/MD/TXT) into a validated `ContractExtractionResult`.
2. Evaluate a captured payment against a milestone and return a `DiscrepancyEvaluationResult` with exact Decimal math and clause-citing prose.

Public surface (`backend/app/ai/__init__.py`), nothing else exported:
```python
async def extract_contract_rules(file_bytes: bytes, filename: str) -> ContractExtractionResult
async def evaluate_payment_compliance(
    contract: ContractExtractionResult, milestone: ExtractedMilestoneSpec,
    captured_amount: Decimal, captured_currency: str, captured_date: date,
    payer_note: str = "",
) -> DiscrepancyEvaluationResult
```

## Boundaries (do not cross)
- No PayPal HTTP / OAuth, no SQLAlchemy / FastAPI routes, no frontend.
- Do not edit `backend/app/schemas/contracts.py` (P4), `requirements.txt` (P1), `.env.example` (P4).
- The LLM never computes money. `compliance_math.py` owns `shortfall_amount`, `penalty_amount`, `total_remediation_amount`.
- No floats in financial paths. Decimal quantized to `0.01`, `ROUND_HALF_UP`.
- No `git init`. Work on feature branches off `main`; Conventional Commits; one PR per branch, P4 review.

## Spec decisions
- Public extractor signature follows the AGENTS.md errata (`file_bytes, filename`), not the older `raw_text` in the plan sheet; `raw_text` stays an internal helper.
- `DiscrepancyEvaluationResult` also carries `discrepancy_status` and `captured_currency` (errata 1 and 11).
- Currency mismatch is **flag-only**: type `CURRENCY_MISMATCH`, status `PENDING_APPROVAL`, shortfall/penalty/total all `0.00`, no FX (matches `globex_currency_mismatch.json`).

## Branch and status
All P2 work lives on one branch: **`feat/p2-ai-engine`**. Commit in the three stages below (one Conventional Commit group each), then open a single PR.

| # | Stage | Contents | Tests | Status |
| --- | --- | --- | --- | --- |
| 1 | Compliance math | `compliance_math.py` | TST-05 `test_compliance_math.py` | Done, committed locally |
| 2 | Contract extractor | `pdf_parser.py`, `extractor.py`, extraction prompts | TST-03, TST-04 | Done, uncommitted; live default chain passes all 3 contracts; OpenAI removed (no credits) |
| 3 | Compliance auditor | `auditor_agent.py`, `prompts.py` guards, shared `llm.py` | TST-05/06/07 | Done, uncommitted; live Gemini passes all 3 scenarios (template covers 503/timeouts) |
| 4 | Public API + checks | `__init__.py` exports, ruff, mypy | full suite | Done, uncommitted: 221 offline tests + 11/12 live pass; ruff and `mypy --strict` clean |
| 5 | Commit + PR | one PR for the branch | CI | Waiting for the go-ahead |

### Stage 1 — compliance math
Rules implemented:
- `shortfall = max(0, expected - captured)`
- `late = captured_date > due_date + grace_period_days`
- `penalty = expected * late_fee% / 100` only if `late` or (`shortfall > 0` and partial payments not allowed), else `0.00`
- `total = shortfall + penalty`
- Type: shortfall and late → `UNDERPAID_AND_LATE`; shortfall only → `UNDERPAID`; late only → `LATE_PAYMENT`; neither → `COMPLIANT`
- Status: `COMPLIANT` → `NONE`, anything else → `PENDING_APPROVAL`

Tests: headline Acme ($150 + $50 = $200), Globex late ($175), Initech partial-allowed, grace-day boundaries, overpayment, zero fee, HALF_UP vs banker's rounding, float rejection, currency mismatch, and a 48-case grid asserting `total == shortfall + penalty`.

### Stage 2 — contract extractor
- `pdf_parser.py`: `extract_text_from_upload(file_bytes, filename)`. `pypdf` for `.pdf` (extension plus `%PDF-` magic), UTF-8 for `.md`/`.txt`. Typed `ContractParseError` for empty, over 5 MB, unsupported type, or unreadable file.
- `extractor.py`: provider chain with an 8 s timeout:
  1. `gemini-3.5-flash`, thinking off (structured output; one retry on 429/5xx). The plan's `gemini-2.5-flash` returns 404 for new API keys.
  2. `gemini-3.1-flash-lite` backup on the same key (one retry on 429/5xx). OpenAI was dropped: no credits available on the team account.
  3. Deterministic regex parser (never crash the demo)
- Post-validation: every `source_clause_quote` and `penalty_clause_quote` must be a substring of the source text; amounts are `Decimal`; failures lower `extraction_confidence` or fall through to the next provider.
- Contract text is wrapped as untrusted data in the prompt.
- Tests: all 3 fixture PDFs match `shared/fixtures/json/*_extract.json` (100% schema validity, under 8 s each); 0-byte file, `.exe`, and a contract with no amounts raise `ContractParseError` (surfaces as HTTP 422 via P1); mocked-provider failover.

### Stage 3 — compliance auditor
- `auditor_agent.py`: `evaluate_payment_compliance(...)` runs the math first; those numbers are final. Gemini only writes `ai_audit_summary` (max 600) and the body of `paypal_invoice_detail_note` (max 1500; goes in invoice draft `detail.note`, never `/send`).
- The note always ends with an engine-written footer: the verbatim violated clause and `Amount due: $200.00 (shortfall $150.00 + late fee $50.00)`.
- `remediation_line_items` come from the math only (`Milestone shortfall - MS-01`, `Late fee (5.00%) - MS-01`; name max 120, description max 500, each cites its section) and always sum to the total.
- Model text is rejected (template used instead) if it states any amount the engine did not compute, omits the total due, leaks internal field names, or breaks length limits.
- Violated clause: the penalty clause when a late fee applies or the payment is late; otherwise the milestone clause (also for currency mismatch, which has no extracted clause of its own).
- Compliant payments: template only, no model call. Currency mismatch: flagged, no line items.
- Budget **3 s** (primary model up to 2.5 s): it runs synchronously on `/capture`, and only the wording is at stake.
- `prompts.py`: versioned prompts; contract clause and payer note in `<untrusted_*>` blocks; every tag-like `<...` inside untrusted text is escaped, so it can neither close its block nor forge `<audit_facts>`.
- `llm.py`: shared Gemini client, transient-error policy and failover loop used by both extractor and auditor.
- Tests: TST-06 (hostile note, even with an obedient model), TST-07 (timeout, hang, 503 ×4, malformed output, no key: all fall back in under 200 ms, or within the 3 s budget for a hanging model).

### Stage 4 — public API
`from app.ai import extract_contract_rules, evaluate_payment_compliance, ContractParseError` (`backend/app/ai/__init__.py`).

## Handoff notes for P1
- `extract_contract_rules` raises `ContractParseError` (`.reason` code, user-safe message): return **HTTP 422** with `str(error)`. It never raises on model failure.
- `evaluate_payment_compliance` never raises on model failure. Pass the capture's own amount, currency and time from the `COMPLETED` capture or verified webhook, never from the browser. A `datetime` is accepted and reduced to its date (pass UTC).
- `contract` may be `ContractExtractionResult`, `PersistedContractExtraction` or `ContractActivateRequest`; `milestone` may be `ExtractedMilestoneSpec` or `PersistedMilestoneSpec`.
- `payer_note`: `SimulatorPaymentRequest` has no note field; pass the PayPal capture/order note if there is one, else omit.
- If `total_remediation_amount == 0` (compliant or currency mismatch) there are no line items: do not create an invoice.
- Use `paypal_invoice_detail_note` as invoice `detail.note` and `remediation_line_items` as `items[]` at **draft creation** (PIT-01).
- Worst-case added latency on `/capture`: 3 s (then template text).

## Order of work
1. ~~Stages 1–4~~ (done).
2. Commit the remaining stages and open one PR for `feat/p2-ai-engine` (when the owner says so).
3. Day 15 (Oct 31): hand off to P1 so `/contracts/extract` and `/simulator/pay` call the real functions.
4. Days 16–22: integration support with P4.

## Dependencies to request from P1 (list in each PR description)
`pypdf==6.21.2`, `google-genai==1.73.0`, `pydantic>=2`, `email-validator`, `pytest`, `pytest-asyncio==0.23.6` (versions verified locally). `python-dotenv` is optional, used only by the live test. `openai` is **not** needed.

For P4 (`.env.example`): `OPENAI_API_KEY` is no longer used; optional new vars `GEMINI_MODEL` (default `gemini-3.5-flash`) and `GEMINI_FALLBACK_MODEL` (default `gemini-3.1-flash-lite`, empty disables).

Live tests (opt-in, a few free-tier Gemini calls): `RUN_LIVE_LLM=1 python -m pytest backend/tests/unit_ai/test_extractor_live.py backend/tests/unit_ai/test_auditor_live.py -v -s`

For P4 (lint config): there is no repo `ruff` config yet. With ruff 0.17 defaults the P2 code passes `ruff check` and `ruff format --check` (line length 88).

## Verification (Definition of Done)
- `python -m pytest backend/tests/unit_ai backend/tests/integration -q`: 221 passed, 12 live tests skipped by default.
- `python -m ruff check backend/app/ai backend/tests/unit_ai && python -m ruff format --check backend/app/ai backend/tests/unit_ai`: clean.
- `python -m mypy --strict backend/app/ai backend/tests/unit_ai`: clean.
- Extraction: 3 fixture contracts, 100% schema pass, exact amounts, under 8 s each.
- Math: zero drift between `shortfall + penalty` and `total_remediation_amount`.
- P1 successfully calls both public functions in the integrated backend.

## Schedule (Africa/Cairo)
- WP-2.1 extractor: Days 4–9 (Oct 20–25), Gate 2 on Oct 25: 3 PDFs parse at 100%.
- WP-2.2 math and auditor: Days 10–15 (Oct 26 – Oct 31).
- All unit suites green in CI by Nov 1; feature freeze Nov 5; internal freeze Nov 11 22:00.
