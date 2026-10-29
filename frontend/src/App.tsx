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
    <div className="flex items-stretch gap-5" role="group" aria-label={label}>
      {items.map((i) => (
        <NavLink
          key={i.to}
          to={i.to}
          className={({ isActive }) =>
            `-mb-px flex items-center whitespace-nowrap border-b-2 text-[14px] transition-colors ${
              isActive ? "border-ink font-medium text-ink" : "border-transparent text-ink-muted hover:text-ink"
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
  const base = "flex items-center gap-2 whitespace-nowrap text-[13px]";
  const dot = (c: string) => <span aria-hidden className={`h-2 w-2 rounded-full ${c}`} />;
  if (apiConfig.useMocks) {
    return (
      <span className={`${base} text-warn`} title="Serving shared/fixtures; no backend or PayPal calls">
        {dot("bg-warn")}
        <span className="hidden sm:inline">Sample data</span>
      </span>
    );
  }
  if (health === null) return <span className={`${base} text-ink-muted`}>Connecting…</span>;
  if (health === "down") return <span className={`${base} text-bad`}>{dot("bg-bad")}API offline</span>;
  const ok = health.paypal_sandbox_connected;
  return (
    <span className={`${base} ${ok ? "text-good" : "text-warn"}`} title={health.ai_provider ? `AI: ${health.ai_provider}` : undefined}>
      {dot(ok ? "bg-good" : "bg-warn")}
      <span className="hidden sm:inline">{ok ? "PayPal sandbox" : "Sandbox not connected"}</span>
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
        <header className="sticky top-0 z-40 border-b border-paper-line bg-white">
          <div className="mx-auto flex max-w-[1440px] flex-wrap items-stretch gap-x-8 px-4 sm:px-6 md:h-14 md:flex-nowrap">
            <NavLink to="/ledger" className="flex h-14 shrink-0 items-center gap-2.5 text-ink">
              <img src="/favicon.svg" alt="" className="h-7 w-7" />
              <span className="whitespace-nowrap text-[15px] font-semibold tracking-[-0.01em]">Financial Detective</span>
            </NavLink>
            <nav
              className="order-last -mx-4 flex h-11 w-[calc(100%+2rem)] min-w-0 items-stretch gap-6 overflow-x-auto border-t border-paper-rule px-4 md:order-none md:mx-0 md:h-auto md:w-auto md:flex-1 md:border-0 md:px-0"
              aria-label="Main"
            >
              <NavGroup label="Detective" items={DETECTIVE_NAV} />
              <span aria-hidden className="my-auto h-4 w-px shrink-0 bg-paper-line" />
              <NavGroup label="Leash" items={LEASH_NAV} />
            </nav>
            <div className="ml-auto flex shrink-0 items-center gap-3 sm:gap-5 md:ml-0">
              <SandboxBadge health={health} />
              <button
                type="button"
                onClick={openUnlock}
                className={`h-8 rounded border px-3 text-[13px] font-medium transition-colors ${
                  unlocked ? "border-paper-line text-ink-soft hover:border-ink-muted" : "border-ink bg-ink text-white hover:bg-brand-dark"
                }`}
              >
                {unlocked ? <><span className="sm:hidden">Unlocked</span><span className="hidden sm:inline">Owner unlocked</span></> : "Unlock"}
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
