import { useEffect, useState, type FormEvent } from "react";
import { getDemoKey, setDemoKey } from "../../api/demoKey";
import { on } from "../../lib/events";
import { apiConfig } from "../../api/client";
import { Modal } from "./Modal";
import { Button } from "./Button";

/**
 * Owner key dialog (plan A.6). Opens from the top bar or automatically on any 401
 * DEMO_KEY_REQUIRED. The key stays in sessionStorage and is sent as X-Demo-Key.
 */
export function UnlockDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [value, setValue] = useState("");
  const [reason, setReason] = useState<string | null>(null);

  useEffect(() => {
    if (open) setValue(getDemoKey() ?? "");
  }, [open]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const key = value.trim();
    if (!key) return;
    setDemoKey(key);
    setReason(null);
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width="sm"
      title="Unlock owner actions"
      subtitle="Reading is open. Uploading, paying and sending need the owner key."
    >
      <form onSubmit={submit} className="space-y-4">
        {reason && <p className="rounded-md bg-warn-soft px-3 py-2 text-sm text-warn">{reason}</p>}
        <label className="block text-sm">
          <span className="font-medium text-ink">Owner key</span>
          <input
            data-autofocus
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="mt-1.5 w-full rounded-md border border-paper-line px-3 py-2 font-mono text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
            placeholder="From the README or Devpost testing notes"
          />
        </label>
        <p className="text-xs text-ink-muted">
          Kept in this browser tab only and sent as the <code className="font-mono">X-Demo-Key</code> header.
          {apiConfig.useMocks && " Mock mode accepts any value."}
        </p>
        <div className="flex justify-end gap-2">
          {getDemoKey() && (
            <Button
              variant="ghost"
              type="button"
              onClick={() => {
                setDemoKey(null);
                onClose();
              }}
            >
              Lock
            </Button>
          )}
          <Button type="submit" disabled={!value.trim()}>
            Unlock
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Hook used by the shell: opens the dialog whenever the API says a key is needed. */
export function useUnlockRequired(open: () => void): void {
  useEffect(() => on("unlock-required", () => open()), [open]);
}
