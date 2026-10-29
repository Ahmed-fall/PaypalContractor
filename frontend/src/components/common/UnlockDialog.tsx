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
      subtitle="Anyone can look around. Uploading, paying and sending invoices need the owner key."
    >
      <form onSubmit={submit} className="space-y-4">
        {reason && <p className="border-l-[3px] border-warn py-1 pl-3 text-[14px] text-warn">{reason}</p>}
        <label className="block text-sm">
          <span className="text-[13px] font-medium text-ink-soft">Owner key</span>
          <input
            data-autofocus
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="mt-1.5 w-full rounded border border-paper-line px-3 py-2 text-[14px] outline-none focus:border-ink"
            placeholder="It is in the README and the Devpost testing notes"
          />
        </label>
        <p className="text-[13px] text-ink-muted">
          Kept in this browser tab only and sent as the <code>X-Demo-Key</code> header.
          {apiConfig.useMocks && " With sample data, any value works."}
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
