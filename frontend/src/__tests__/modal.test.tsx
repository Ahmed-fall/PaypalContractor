import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Modal } from "../components/common/Modal";

function Harness({ autofocus = false }: { autofocus?: boolean }) {
  const [open, setOpen] = useState(false);
  const [inner, setInner] = useState(false);
  return (
    <>
      <div aria-live="polite" data-testid="live" />
      <main>
        <button type="button">Grid action</button>
        <button type="button" onClick={() => setOpen(true)}>
          Pay team
        </button>
      </main>
      <Modal
        open={open}
        title="Pay the team"
        subtitle="This can't be undone."
        onClose={() => setOpen(false)}
        footer={
          <>
            <button type="button" onClick={() => setInner(true)}>
              More
            </button>
            <button type="button" data-autofocus={autofocus || undefined}>
              Send payment
            </button>
          </>
        }
      >
        <input aria-label="Note" />
        <Modal open={inner} title="Unlock" onClose={() => setInner(false)}>
          <input aria-label="Owner key" />
        </Modal>
      </Modal>
    </>
  );
}

async function openDialog(autofocus = false) {
  const user = userEvent.setup();
  render(<Harness autofocus={autofocus} />);
  const trigger = screen.getByRole("button", { name: "Pay team" });
  await user.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "Pay the team" });
  return { user, trigger, dialog };
}

describe("Modal focus management (#25)", () => {
  it("labels the dialog from its title and subtitle", async () => {
    const { dialog } = await openDialog();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Pay the team");
    expect(dialog).toHaveAccessibleDescription("This can't be undone.");
  });

  it("moves focus to the first focusable element on open", async () => {
    const { dialog } = await openDialog();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus());
  });

  it("honours data-autofocus over document order", async () => {
    const { dialog } = await openDialog(true);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Send payment" })).toHaveFocus());
  });

  it("keeps Tab and Shift+Tab inside the dialog", async () => {
    const { user, dialog } = await openDialog();
    const close = within(dialog).getByRole("button", { name: "Close" });
    const send = within(dialog).getByRole("button", { name: "Send payment" });
    await waitFor(() => expect(close).toHaveFocus());

    await user.tab({ shift: true });
    expect(send).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();

    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("makes the background inert and hidden, but not the dialog or live regions", async () => {
    const { dialog } = await openDialog();
    const grid = screen.getByRole("button", { name: "Grid action", hidden: true });
    expect(grid.closest("[inert]")).not.toBeNull();
    expect(grid.closest("[aria-hidden='true']")).not.toBeNull();
    expect(dialog.closest("[inert]")).toBeNull();
    expect(screen.getByTestId("live").closest("[inert]")).toBeNull();
  });

  it("returns focus to the opener and clears inert on close", async () => {
    const { user, trigger } = await openDialog();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(document.querySelector("[inert]")).toBeNull();
    expect(document.querySelector("[aria-hidden='true']")).toBeNull();
  });

  it("only the topmost of stacked dialogs is interactive", async () => {
    const { user, dialog } = await openDialog();
    const more = within(dialog).getByRole("button", { name: "More" });
    await user.click(more);
    const unlock = await screen.findByRole("dialog", { name: "Unlock" });

    const send = within(dialog).getByRole("button", { name: "Send payment", hidden: true });
    expect(send.closest("[inert]")).not.toBeNull();
    expect(unlock.closest("[inert]")).toBeNull();
    await waitFor(() => expect(within(unlock).getByRole("button", { name: "Close" })).toHaveFocus());
    await user.tab();
    expect(unlock.contains(document.activeElement)).toBe(true);

    // Closing the top dialog hands control back to the one below, not to the page.
    await user.click(within(unlock).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Unlock" })).not.toBeInTheDocument());
    expect(send.closest("[inert]")).toBeNull();
    expect(more).toHaveFocus();
    expect(screen.getByRole("button", { name: "Grid action", hidden: true }).closest("[inert]")).not.toBeNull();
  });
});
