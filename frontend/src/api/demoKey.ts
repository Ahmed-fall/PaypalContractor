/**
 * The owner key (plan A.6). Never bundled: the Unlock dialog asks for it once and it
 * lives in sessionStorage for this tab only. Storage can be unavailable (private
 * mode, sandboxed previews), so every access is guarded and falls back to memory.
 */
import { emit } from "../lib/events";

const STORAGE_KEY = "fd.demoKey";
let memoryKey: string | null = null;

export function getDemoKey(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? memoryKey;
  } catch {
    return memoryKey;
  }
}

export function setDemoKey(key: string | null): void {
  memoryKey = key;
  try {
    if (key) sessionStorage.setItem(STORAGE_KEY, key);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* memory fallback already set */
  }
  emit("demo-key-changed", Boolean(key));
}
