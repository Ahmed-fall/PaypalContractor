import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { ToastProvider } from "./components/common/Toast";
import { UnlockDialog, useUnlockRequired } from "./components/common/UnlockDialog";
import { api, apiConfig } from "./api/client";
import { getDemoKey } from "./api/demoKey";
import { on } from "./lib/events";
import type { HealthResponse } from "./types/contracts";
import ContractsPage from "./pages/ContractsPage";
import ContractWorkbenchPage from "./pages/ContractWorkbenchPage";
import SimulatorPage from "./pages/SimulatorPage";
import LedgerPage from "./pages/LedgerPage";
import LeashPlaceholder from "./pages/LeashPlaceholder";
import NotFound from "./pages/NotFound";

const DETECTIVE_NAV = [
  { to: "/contracts", label: "Contracts" },
  { to: "/simulator", label: "Simulator" },
  { to: "/ledger", label: "Ledger" },
];
const LEASH_NAV = [
  { to: "/leash/rules", label: "Rules" },
  { to: "/leash/team", label: "Team" },
  { to: "/leash/payroll", label: "Payroll" },
  { to: "/leash/decisions", label: "Decisions" },
];

function NavGroup({ label, items }: { label: string; items: { to: string; label: string }[] }) {
  return (
    <div className="flex items-center gap-1">
      <span className="mr-1 hidden text-[11px] font-semibold uppercase tracking-wider text-slate-400 lg:inline">{label}</span>
      {items.map((i) => (
        <NavLink
          key={i.to}
          to={i.to}
          className={({ isActive }) =>
            `whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm font-medium transition ${
              isActive ? "bg-white/10 text-white" : "text-slate-300 hover:bg-white/5 hover:text-white"
            }`
          }
        >
          {i.label}
        </NavLink>
      ))}
    </div>
  );
}

function SandboxBadge({ health }: { health: HealthResponse | null | "down" }) {
  if (apiConfig.useMocks) {
    return (
      <span className="rounded-full bg-amber-400/15 px-2.5 py-1 text-xs font-medium text-amber-300 ring-1 ring-inset ring-amber-300/30" title="Serving shared/fixtures; no backend or PayPal calls">
        Mock data
      </span>
    );
  }
  if (health === null) return <span className="text-xs text-slate-400">Checking…</span>;
  if (health === "down") {
    return <span className="rounded-full bg-red-400/15 px-2.5 py-1 text-xs font-medium text-red-300 ring-1 ring-inset ring-red-300/30">API offline</span>;
  }
  const ok = health.paypal_sandbox_connected;
  return (
    <span
      className={`rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${
        ok ? "bg-emerald-400/15 text-emerald-300 ring-emerald-300/30" : "bg-amber-400/15 text-amber-300 ring-amber-300/30"
      }`}
      title={health.ai_provider ? `AI: ${health.ai_provider}` : undefined}
    >
      PayPal sandbox{ok ? "" : " (not connected)"}
    </span>
  );
}

export default function App() {
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlocked, setUnlocked] = useState(Boolean(getDemoKey()));
  const [health, setHealth] = useState<HealthResponse | null | "down">(null);
  const openUnlock = useCallback(() => setUnlockOpen(true), []);
  useUnlockRequired(openUnlock);

  useEffect(() => on("demo-key-changed", setUnlocked), []);
  useEffect(() => {
    let alive = true;
    api
      .health()
      .then((h) => alive && setHealth(h))
      .catch(() => alive && setHealth("down"));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <ToastProvider>
      <div className="min-h-screen">
        <header className="sticky top-0 z-40 bg-ink">
          <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-x-4 px-4 sm:px-6 md:h-14 md:flex-nowrap">
            <NavLink to="/ledger" className="flex h-14 shrink-0 items-center gap-2 text-white">
              <img src="/favicon.svg" alt="" className="h-7 w-7" />
              <span className="hidden text-sm font-semibold tracking-tight sm:inline">The Financial Detective</span>
            </NavLink>
            <nav className="order-last -mx-4 flex w-[calc(100%+2rem)] min-w-0 items-center gap-4 overflow-x-auto border-t border-white/10 px-4 py-2 md:order-none md:mx-0 md:w-auto md:flex-1 md:border-0 md:px-0 md:py-0" aria-label="Main">
              <NavGroup label="Detective" items={DETECTIVE_NAV} />
              <span aria-hidden className="h-5 w-px shrink-0 bg-white/15" />
              <NavGroup label="Leash" items={LEASH_NAV} />
            </nav>
            <div className="ml-auto flex shrink-0 items-center gap-3 md:ml-0">
              <SandboxBadge health={health} />
              <button
                type="button"
                onClick={openUnlock}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ring-1 ring-inset transition ${
                  unlocked ? "text-emerald-300 ring-emerald-300/30 hover:bg-white/5" : "bg-white text-ink ring-white hover:bg-slate-100"
                }`}
              >
                {unlocked ? "Unlocked" : "Unlock"}
              </button>
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-[1440px] px-4 py-6 sm:px-6 sm:py-8">
          <Routes>
            <Route path="/" element={<Navigate to="/ledger" replace />} />
            <Route path="/contracts" element={<ContractsPage />} />
            <Route path="/contracts/new" element={<ContractWorkbenchPage />} />
            <Route path="/contracts/:contractId" element={<ContractWorkbenchPage />} />
            <Route path="/simulator" element={<SimulatorPage />} />
            <Route path="/ledger" element={<LedgerPage />} />
            <Route path="/leash/rules" element={<LeashPlaceholder title="Rules" route="/leash/rules" />} />
            <Route path="/leash/team" element={<LeashPlaceholder title="Team" route="/leash/team" />} />
            <Route path="/leash/payroll" element={<LeashPlaceholder title="Payroll" route="/leash/payroll" />} />
            <Route path="/leash/payroll/:runId" element={<LeashPlaceholder title="Payroll run" route="/leash/payroll/:runId" />} />
            <Route path="/leash/decisions" element={<LeashPlaceholder title="Decisions" route="/leash/decisions" />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
      <UnlockDialog open={unlockOpen} onClose={() => setUnlockOpen(false)} />
    </ToastProvider>
  );
}
