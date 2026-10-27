import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, apiConfig, isApiError } from "../api/client";
import { ConfidenceBadge, RulesEditor, SourcePane, UploadPanel, toActivateRequest, validateTerms, type UploadInput } from "../components/ContractWorkbench";
import { Button, Spinner } from "../components/common/Button";
import { PageHeader } from "../components/common/Card";
import { Pill } from "../components/common/StatusBadge";
import { useToast } from "../components/common/Toast";
import { getContract, saveExtracted, updateContract, type StoredContract } from "../lib/contractStore";
import { pendingFiles } from "../lib/pendingFiles";
import type { PersistedContractExtraction } from "../types/contracts";

async function readText(file: File): Promise<string | null> {
  if (!/\.(md|txt)$/i.test(file.name)) return null;
  try {
    return await file.text();
  } catch {
    return null;
  }
}

function extractionErrorMessage(e: unknown): string {
  if (!isApiError(e)) return "Something went wrong while reading the contract.";
  switch (e.code) {
    case "FILE_TOO_LARGE":
    case "UNSUPPORTED_FILE_TYPE":
      return e.message;
    case "EXTRACTION_FAILED":
      return "The AI could not read this contract reliably. Try pasting the text instead.";
    case "AI_RATE_LIMITED": {
      const s = typeof e.details?.retry_after_s === "number" ? ` in ${e.details.retry_after_s} s` : " in a moment";
      return `The AI providers are busy. Try again${s}.`;
    }
    case "DEMO_KEY_REQUIRED":
      return "Unlock the demo with the owner key to upload a contract.";
    default:
      return e.requestId ? `${e.message} (request ${e.requestId})` : e.message;
  }
}

/** Upload step: /contracts/new */
function NewContract() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [samples, setSamples] = useState<{ label: string; load: () => File | null }[]>([]);

  useEffect(() => {
    if (import.meta.env.VITE_USE_MOCKS !== "true" || !apiConfig.useMocks) return;
    let alive = true;
    import("../api/mocks").then(({ fixtureContractText, FIXTURE_STEMS }) => {
      if (!alive) return;
      setSamples(
        FIXTURE_STEMS.flatMap((stem) => {
          const text = fixtureContractText(stem);
          return text ? [{ label: stem.replace(/_/g, " "), load: () => new File([text], `${stem}.md`, { type: "text/markdown" }) }] : [];
        }),
      );
    });
    return () => {
      alive = false;
    };
  }, []);

  const submit = async (input: UploadInput) => {
    setBusy(true);
    setError(null);
    try {
      const res = input.file ? await api.extractContract({ file: input.file }) : await api.extractContract({ raw_text: input.rawText ?? "" });
      const text = input.file ? await readText(input.file) : (input.rawText ?? null);
      saveExtracted(res, text);
      if (input.file) pendingFiles.set(res.contract_id, input.file);
      navigate(`/contracts/${res.contract_id}`);
    } catch (e) {
      setError(extractionErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        eyebrow="Step 1 · Contract"
        title="Upload a signed contract"
        description="The AI reads the legal clauses and turns them into payment rules: milestones, due dates, grace period and late fee. You review every field before anything is monitored."
      />
      <UploadPanel busy={busy} error={error} onSubmit={submit} samples={samples} />
      <ul className="mt-6 grid gap-3 text-xs text-ink-muted sm:grid-cols-3">
        <li className="rounded-lg border border-paper-line bg-white p-3">
          <span className="font-semibold text-ink">AI extracts.</span> Each rule links back to the clause it came from.
        </li>
        <li className="rounded-lg border border-paper-line bg-white p-3">
          <span className="font-semibold text-ink">You confirm.</span> Nothing is enforced until you activate.
        </li>
        <li className="rounded-lg border border-paper-line bg-white p-3">
          <span className="font-semibold text-ink">Code decides.</span> Amounts owed are always computed in code, never by the AI.
        </li>
      </ul>
    </div>
  );
}

/** Review / active step: /contracts/:contractId */
function ReviewContract({ contractId }: { contractId: string }) {
  const toast = useToast();
  const navigate = useNavigate();
  const [record, setRecord] = useState<StoredContract | null>(() => getContract(contractId));
  const [terms, setTerms] = useState<PersistedContractExtraction | null>(record?.extraction ?? null);
  const [editing, setEditing] = useState(record?.status !== "ACTIVE");
  const [busy, setBusy] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const file = pendingFiles.get(contractId);
  const [aiValues] = useState<PersistedContractExtraction | null>(record?.extraction ?? null);

  const errors = useMemo(() => (terms ? validateTerms(terms) : {}), [terms]);
  const errorCount = Object.keys(errors).length;

  const activate = useCallback(async () => {
    if (!terms) return;
    setShowErrors(true);
    if (errorCount > 0) {
      toast.show(`Fix ${errorCount} field${errorCount > 1 ? "s" : ""} before activating.`, "error");
      return;
    }
    setBusy(true);
    try {
      const res = await api.activateContract(contractId, toActivateRequest(terms));
      const extraction = { ...terms, milestones: res.milestones };
      const next = updateContract(contractId, { status: res.status, extraction });
      setRecord(next);
      setTerms(extraction);
      setEditing(false);
      setShowErrors(false);
      toast.show("Contract activated. Payments against it are now audited.", "success");
    } catch {
      /* the client already raised the toast or the unlock dialog */
    } finally {
      setBusy(false);
    }
  }, [terms, errorCount, contractId, toast]);

  if (!record || !terms) {
    return (
      <div className="mx-auto max-w-lg py-16 text-center">
        <h1 className="text-xl font-semibold text-ink">Contract not found in this tab</h1>
        <p className="mt-2 text-sm text-ink-muted">
          Reviewed contracts are kept in this browser tab. Upload it again to review or edit its terms.
        </p>
        <Link to="/contracts/new" className="mt-6 inline-block text-sm font-medium text-brand hover:underline">
          Upload a contract
        </Link>
      </div>
    );
  }

  const active = record.status === "ACTIVE";
  return (
    <div>
      <PageHeader
        eyebrow={active ? "Contract · monitoring" : "Step 2 · Review"}
        title={terms.contract_title || "Untitled contract"}
        description={
          active
            ? "These terms are locked in. Every PayPal payment against these milestones is audited automatically."
            : "Check each rule against the highlighted clauses, fix anything the AI got wrong, then activate."
        }
        actions={
          <>
            {active ? <Pill tone="good">Active</Pill> : <Pill tone="neutral">Draft</Pill>}
            <ConfidenceBadge value={record.extraction.extraction_confidence} />
          </>
        }
      />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <div className="lg:sticky lg:top-20 lg:h-[calc(100vh-7rem)]">
          <SourcePane
            filename={record.source_filename}
            file={file}
            text={record.source_text}
            milestoneQuotes={terms.milestones.map((m) => m.source_clause_quote)}
            penaltyQuote={terms.penalty_clause_quote}
          />
        </div>

        <div className="rounded-xl border border-paper-line bg-white shadow-card">
          <div className="p-5 sm:p-6">
            <RulesEditor value={terms} onChange={setTerms} errors={showErrors ? errors : {}} readOnly={!editing || busy} aiValues={aiValues} />
          </div>
          <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 rounded-b-xl border-t border-paper-line bg-white/95 px-5 py-4 backdrop-blur sm:px-6">
            <p className="text-xs text-ink-muted">
              {editing
                ? showErrors && errorCount
                  ? `${errorCount} field${errorCount > 1 ? "s" : ""} need attention.`
                  : "Nothing is monitored until you activate."
                : `Contract ${contractId}`}
            </p>
            <div className="flex flex-wrap gap-2">
              {active && !editing && (
                <>
                  <Button variant="secondary" onClick={() => setEditing(true)}>
                    Edit terms
                  </Button>
                  <Button onClick={() => navigate(`/simulator?milestone=${encodeURIComponent(terms.milestones[0]?.milestone_id ?? "")}`)}>
                    Simulate a payment
                  </Button>
                </>
              )}
              {editing && (
                <>
                  {active && (
                    <Button
                      variant="ghost"
                      disabled={busy}
                      onClick={() => {
                        setTerms(record.extraction);
                        setEditing(false);
                        setShowErrors(false);
                      }}
                    >
                      Cancel
                    </Button>
                  )}
                  <Button onClick={activate} disabled={busy}>
                    {busy && <Spinner />}
                    {active ? "Save and re-activate" : "Activate Contract Monitoring"}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ContractWorkbenchPage() {
  const { contractId } = useParams();
  return contractId ? <ReviewContract key={contractId} contractId={contractId} /> : <NewContract />;
}
