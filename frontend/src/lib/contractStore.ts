/**
 * Keeps contracts the user extracted in this tab so /contracts/:contractId survives a
 * refresh. There is no GET /contracts/{id} in the API (plan 7.5), so this is the only
 * client copy; it never holds secrets. Falls back to memory if storage is blocked.
 */
import type { ContractExtractResponse, ContractStatus, PersistedContractExtraction } from "../types/contracts";

export interface StoredContract {
  contract_id: string;
  status: ContractStatus;
  source_filename: string;
  extraction: PersistedContractExtraction;
  source_text: string | null; // .md/.txt content, shown in the text pane
  updated_at: string;
}

const KEY = "fd.contracts";
let memory: Record<string, StoredContract> = {};

function readAll(): Record<string, StoredContract> {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Record<string, StoredContract>) : memory;
  } catch {
    return memory;
  }
}

function writeAll(all: Record<string, StoredContract>): void {
  memory = all;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* memory copy is enough */
  }
}

export function saveExtracted(res: ContractExtractResponse, sourceText: string | null): StoredContract {
  const rec: StoredContract = {
    contract_id: res.contract_id,
    status: res.status,
    source_filename: res.source_filename,
    extraction: res.extraction,
    source_text: sourceText,
    updated_at: new Date().toISOString(),
  };
  writeAll({ ...readAll(), [rec.contract_id]: rec });
  return rec;
}

export function updateContract(id: string, patch: Partial<StoredContract>): StoredContract | null {
  const all = readAll();
  const cur = all[id];
  if (!cur) return null;
  const next = { ...cur, ...patch, updated_at: new Date().toISOString() };
  writeAll({ ...all, [id]: next });
  return next;
}

export function getContract(id: string): StoredContract | null {
  return readAll()[id] ?? null;
}

export function listContracts(): StoredContract[] {
  return Object.values(readAll()).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
