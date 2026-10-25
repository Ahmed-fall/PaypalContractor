# Fixtures — The Financial Detective

Owner: **Person 4**. Consumers: P1 (backend), P2 (AI), P3 (frontend).

- `contracts/` — the 3 canonical sample contracts (`.md` source + generated `.pdf`).
- `json/` — deterministic expected payloads matching `backend/app/schemas/contracts.py`.
- `leash/` — Leash roster, policy and timesheet fixtures (owned by **P5**; P4 reviews
  the schema they target, not the files).

> Frozen names (plan §12, Table 55 / WP-4.1): `Acme_Standard`, `Globex_LateFee`,
> `Initech_MultiMilestone`. Do not rename.

## Scenario matrix

| Contract | Client / payer | Milestone(s) | Late fee | Grace | Partial | Exercise |
| --- | --- | --- | --- | --- | --- | --- |
| **Acme_Standard.pdf** | Acme Corp · `sb-buyer@personal.example.com` | MS-01 `$1,000.00` due `2026-10-15` | 5.00% | 3 d | forbidden | headline demo: `$850.00` paid `2026-10-20` → **UNDERPAID_AND_LATE** ($150 + $50 = $200) |
| **Globex_LateFee.pdf** | Globex Industries Inc. · `finance@globex.example.com` | MS-01 `$2,500.00` due `2026-11-01` | 7.00% | 0 d | forbidden | full amount paid late → **LATE_PAYMENT** (penalty $175) |
| **Initech_MultiMilestone.pdf** | Initech LLC · `ap@initech.example.com` | MS-01 `$1,200.00` · MS-02 `$1,800.00` · MS-03 `$900.00` | 5.00% | 5 d | permitted | mixed ledger: **COMPLIANT** + **UNDERPAID** + pending |

The Globex contract also drives the `CURRENCY_MISMATCH` fixture: the same `$2,500.00`
milestone settled in `EUR`, against its Section 6.1 USD-settlement clause.

Amounts are Python `Decimal` serialized as strings (`"1000.00"`). `DiscrepancyType`
(why) and `DiscrepancyStatus` (remediation lifecycle) are orthogonal — see §12.

`captured_currency` (optional ISO-4217) records the currency the payment actually
arrived in; the row's `currency` stays the contract/billing currency for every other
amount. Foreign-currency captures are excluded from `total_captured` — there is no FX
conversion. How a `CURRENCY_MISMATCH` is remediated (flag-only vs re-invoice the full
milestone) is decided in `compliance_math.py` by P2.

## DiscrepancyType coverage

| `DiscrepancyType` | Fixture |
| --- | --- |
| `COMPLIANT` | `initech_ledger.json` (MS-01) |
| `UNDERPAID` | `initech_ledger.json` (MS-02) |
| `LATE_PAYMENT` | `globex_ledger.json` |
| `UNDERPAID_AND_LATE` | `acme_ledger.json`, `acme_simulate_response.json` |
| `CURRENCY_MISMATCH` | `globex_currency_mismatch.json` |

## JSON files

| File | Model |
| --- | --- |
| `json/acme_extract.json`, `globex_extract.json`, `initech_extract.json` | `ContractExtractResponse` |
| `json/acme_ledger.json`, `globex_ledger.json`, `globex_currency_mismatch.json`, `initech_ledger.json` | `LedgerResponse` |
| `json/acme_simulate_request.json` | `SimulatorPaymentRequest` |
| `json/acme_simulate_response.json` | `SimulatorPaymentResponse` |
| `json/acme_remediation_response.json` | `RemediationResponse` |

## Conventions

- `contract_id` uses the plan's `cnt_…` form; milestone ids `ms_…`; discrepancies `dsc_…`.
- Regenerate the PDFs after editing a `.md`: `python3 scripts/build_fixture_pdfs.py`.
- Conformance is enforced by `backend/tests/integration/test_fixture_contracts.py`
  (`pytest backend/tests/integration`). Editing a schema without updating fixtures fails CI.
