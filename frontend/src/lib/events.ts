/** Tiny typed event bus shared by the API client and app-level UI (toast, unlock). */
import type { ApiError } from "../api/errors";

type Events = {
  "api-error": ApiError;
  "unlock-required": ApiError;
  "demo-key-changed": boolean;
  "ledger-changed": void;
};

type Handler<T> = (payload: T) => void;
const handlers = new Map<keyof Events, Set<Handler<never>>>();

export function on<K extends keyof Events>(event: K, fn: Handler<Events[K]>): () => void {
  let set = handlers.get(event);
  if (!set) {
    set = new Set();
    handlers.set(event, set);
  }
  const s = set as Set<Handler<Events[K]>>;
  s.add(fn);
  return () => {
    s.delete(fn);
  };
}

export function emit<K extends keyof Events>(event: K, payload: Events[K]): void {
  const set = handlers.get(event) as Set<Handler<Events[K]>> | undefined;
  set?.forEach((fn) => fn(payload));
}
