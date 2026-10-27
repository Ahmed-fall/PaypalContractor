# Frontend: The Financial Detective (P3 · Moamen)

React 18 + TypeScript + Vite + Tailwind + AG Grid Community (`ag-grid-react`). Plan Rev 2.3, WP-3.1 to WP-3.4.

## Run it

```bash
cd frontend
npm install
cp .env.example .env          # VITE_USE_MOCKS=true serves shared/fixtures with 400 ms latency
npm run dev                   # http://localhost:5173
```

Against the real backend set `VITE_USE_MOCKS=false` and `VITE_API_BASE_URL=http://localhost:8000`.

| Command | What it checks |
| --- | --- |
| `npm run build` | `tsc --noEmit` then a production build |
| `npm run lint` | ESLint, `no-explicit-any` is an error |
| `npm test` | Vitest: TST-11 (double-click lock, QR fallback, $850 flow, null-safe margin totals in cents) |

Mock mode needs `shared/` next to `frontend/`. Production builds never include the mock layer or fixtures (compile-time check in `src/api/client.ts`), so the Docker build with only `frontend/` as context works.

## Pages (plan A.7)

| Route | Component | Notes |
| --- | --- | --- |
| `/contracts`, `/contracts/new`, `/contracts/:id` | `ContractWorkbench` | Drag and drop upload, split view (source with cited clauses highlighted, editable rules), Activate Contract Monitoring |
| `/simulator` | `PaymentSimulator` | Presets A (full, on time), B (85%, five days late: $850 on Acme), C (custom amount, date, currency) |
| `/ledger` | `ReconciliationGrid` + `RemediationModal` at `?discrepancy=dsc_…` | Pinned totals, quick filter, status pills, expandable detail row, margin columns |
| `/leash/*` | placeholders | Person 5 replaces them with `src/components/Leash/*` |

## Conventions for shared screens (for P5's Leash pages)

- **API:** call `api.*` from `src/api/client.ts` only. It adds `X-Demo-Key` on writes, turns every non-2xx into an `ApiError` (A.4), raises the single toast, and opens the Unlock dialog on 401. Pass `{ quiet: true }` when you show the error inline. Every Leash endpoint is already exposed and mocked.
- **Money:** strings on the wire, never floats. Use `toCents`, `sumMoney`, `formatMoney` from `src/lib/money.ts`. Missing values coalesce to `0`, so totals cannot show `NaN` or `849.9999`.
- **Grid look:** wrap grids in `<div className="ag-theme-quartz fd-grid">`; put `className: "num"` (or `cellClass: "num"`) on money columns; row height 58; status chips from `src/components/common/StatusBadge.tsx` (`Pill`, `DecisionBadge`, `PayrollBadge`).
- **Community only:** no Enterprise modules. Expandable rows use a full-width row, not master/detail, so there is no watermark.
- **Pinned columns:** pin only on wide screens (`useMediaQuery("(min-width: 900px)")`); AG Grid drops pinned columns that do not fit on phones.
- **Never** call PayPal from the browser or put the demo key in the bundle.

## Known gaps

- `client_tags` (G-03) has no field in `ContractActivateRequest` yet (P4 owns `types/contracts.ts`), so the workbench cannot edit tags.
- There is no `GET /contracts/{id}`; reviewed contracts are kept in this browser tab (`src/lib/contractStore.ts`).
- The Leash UI review (WP-3.4) waits on P5's pages landing in `src/components/Leash/`.
