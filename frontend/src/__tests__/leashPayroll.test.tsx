import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { on } from "../lib/events";
import { setTransport, type Transport, type TransportRequest } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import PayrollPage from "../components/Leash/PayrollPage";
import { mappingProblem } from "../components/Leash/mapping";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function RunProbe() {
  const { runId } = useParams();
  return <p>Run page {runId}</p>;
}

function renderPayroll() {
  return render(
    <MemoryRouter future={router} initialEntries={["/leash/payroll"]}>
      <Routes>
        <Route path="/leash/payroll" element={<PayrollPage />} />
        <Route path="/leash/payroll/:runId" element={<RunProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

const csv = (name = "hours.csv", body = "a,b\n1,2\n") => new File([body], name, { type: "text/csv" });

/** Wrap the mock so a test can change one answer and see every call. */
function spyTransport(override?: (req: TransportRequest) => ReturnType<Transport> | null) {
  const calls: TransportRequest[] = [];
  const t: Transport = async (req) => {
    calls.push(req);
    return (await override?.(req)) ?? mockTransport(req);
  };
  setTransport(t);
  return calls;
}

describe("Leash Payroll home (WP-5.4 UI)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("mapping dialog pre-fills proposal and blocks duplicate targets", async () => {
    const calls = spyTransport();
    const user = userEvent.setup();
    renderPayroll();
    await user.upload(screen.getByLabelText("Timesheet file"), csv());

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/Employee/)).toHaveValue("member");
    expect(within(dialog).getByLabelText(/Hrs/)).toHaveValue("hours");
    expect(within(dialog).getByText("97% sure")).toBeInTheDocument();
    expect(within(dialog).getByText("81% sure")).toBeInTheDocument();

    // Day -> hours as well: two columns for one field
    await user.selectOptions(within(dialog).getByLabelText(/Day/), "hours");
    await user.click(within(dialog).getByRole("button", { name: "Confirm mapping" }));
    expect(await within(dialog).findByText("Only one column can be Hours.")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/mapping"))).toBe(false);

    // Hrs -> ignore leaves "hours" once but no work_date: a missing required field
    await user.selectOptions(within(dialog).getByLabelText(/Hrs/), "");
    expect(await within(dialog).findByText("Choose a column for: Work date.")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/mapping"))).toBe(false);

    await user.selectOptions(within(dialog).getByLabelText(/Day/), "work_date");
    await user.selectOptions(within(dialog).getByLabelText(/Hrs/), "hours");
    await user.click(within(dialog).getByRole("button", { name: "Confirm mapping" }));
    expect(await screen.findByText("9 rows for MS-01")).toBeInTheDocument();
    const sent = calls.find((c) => c.path.endsWith("/mapping"))!;
    expect(sent.body).toEqual({
      mapping: { Employee: "member", Day: "work_date", Hrs: "hours", Project: "milestone_code" },
    });
  });

  it("mappingProblem names missing required fields", () => {
    expect(mappingProblem({ A: "member", B: "hours" })).toBe("Choose a column for: Work date, Milestone code.");
    expect(mappingProblem({ A: "member", B: "work_date", C: "hours", D: "milestone_code", E: "rate" })).toBeNull();
  });

  it("parsed upload shows counts and unknown members", async () => {
    spyTransport((req) =>
      req.method === "POST" && req.path === "/leash/timesheets"
        ? Promise.resolve({
            status: 201,
            body: { upload_id: "tsu_9", status: "PARSED", entries: 12, unknown_members: ["Zed", "Quinn"], milestones: ["MS-01", "MS-02"], warnings: [] },
          })
        : null,
    );
    const user = userEvent.setup();
    renderPayroll();
    await user.upload(screen.getByLabelText("Timesheet file"), csv());
    expect(await screen.findByText("12 rows for MS-01, MS-02")).toBeInTheDocument();
    expect(screen.getByText("Not in your team: Zed, Quinn")).toBeInTheDocument();
  });

  it("oversize file rejected client-side", async () => {
    const calls = spyTransport();
    const user = userEvent.setup();
    renderPayroll();
    const big = new File([new Uint8Array(2 * 1024 * 1024 + 1)], "big.csv", { type: "text/csv" });
    await user.upload(screen.getByLabelText("Timesheet file"), big);
    expect(await screen.findByText("This timesheet is larger than 2 MB.")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/leash/timesheets")).toBe(false);
    expect(screen.getByLabelText("Timesheet file")).toBeEnabled();
  });

  it("test_upload_errors_are_inline", async () => {
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const answer = { current: 422 };
    spyTransport((req) => {
      if (req.method !== "POST" || req.path !== "/leash/timesheets") return null;
      const bodies: Record<number, object> = {
        422: { code: "TIMESHEET_INVALID", message: "Some timesheet rows could not be read.", details: { rows: [{ row: 3, error: "hours must be a number" }, { row: 7, error: "unknown date" }] } },
        415: { code: "UNSUPPORTED_FILE_TYPE", message: "Upload a .csv or .xlsx timesheet.", details: null },
        413: { code: "FILE_TOO_LARGE", message: "This timesheet is larger than 2 MB.", details: null },
      };
      return Promise.resolve({ status: answer.current, body: { error: { ...bodies[answer.current], request_id: "r1" } } });
    });
    const user = userEvent.setup({ applyAccept: false });
    renderPayroll();
    const input = screen.getByLabelText("Timesheet file");

    await user.upload(input, csv());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Some timesheet rows could not be read.");
    const rows = within(alert).getByRole("list", { name: "Rows to fix" });
    expect(within(rows).getByText("Row 3: hours must be a number")).toBeInTheDocument();
    expect(within(rows).getByText("Row 7: unknown date")).toBeInTheDocument();
    expect(input).toHaveAccessibleDescription(/Some timesheet rows could not be read/);

    answer.current = 415;
    await user.upload(input, new File(["x"], "notes.txt", { type: "text/plain" }));
    expect(await screen.findByText("Upload a .csv or .xlsx timesheet.")).toBeInTheDocument();
    expect(screen.queryByText("Row 3: hours must be a number")).not.toBeInTheDocument(); // old result cleared

    answer.current = 413;
    await user.upload(input, csv("again.csv"));
    expect(await screen.findByText("This timesheet is larger than 2 MB.")).toBeInTheDocument();

    expect(input).toBeEnabled(); // page stays usable
    expect(screen.getByRole("button", { name: "Prepare payroll" })).toBeDisabled(); // only because no milestone is chosen
    expect(toasts).toEqual([]); // never a second, global error
    off();
  });

  it("discard upload clears the pending mapping", async () => {
    const calls = spyTransport();
    const user = userEvent.setup();
    renderPayroll();
    await user.upload(screen.getByLabelText("Timesheet file"), csv());
    await user.click(await screen.findByRole("button", { name: "Discard upload" }));
    expect(await screen.findByText("Upload discarded. Choose another file.")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/discard"))).toBe(true);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("prepare navigates to the run", async () => {
    const calls = spyTransport();
    const user = userEvent.setup();
    renderPayroll();
    const select = await screen.findByLabelText("Milestone");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    const option = within(select).getAllByRole("option")[1] as HTMLOptionElement;
    await user.selectOptions(select, option.value);
    await user.click(screen.getByRole("button", { name: "Prepare payroll" }));
    expect(await screen.findByText(/^Run page /)).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.path === "/leash/payroll/runs")!;
    expect(post.body).toEqual({ mode: "MILESTONE", milestone_id: option.value });
  });

  it("prepare errors are shown inline", async () => {
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const answer = { current: 409 };
    spyTransport((req) => {
      if (req.method !== "POST" || req.path !== "/leash/payroll/runs") return null;
      return Promise.resolve(
        answer.current === 409
          ? { status: 409, body: { error: { code: "MAPPING_REQUIRED", message: "A timesheet still needs its column mapping.", details: { upload_id: "tsu_3" }, request_id: "r" } } }
          : { status: 404, body: { error: { code: "NOT_FOUND", message: "No timesheet hours for this milestone.", details: null, request_id: "r" } } },
      );
    });
    const user = userEvent.setup();
    renderPayroll();
    const select = await screen.findByLabelText("Milestone");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(select, (within(select).getAllByRole("option")[1] as HTMLOptionElement).value);
    await user.click(screen.getByRole("button", { name: "Prepare payroll" }));
    expect(await screen.findByText("Finish or discard the pending timesheet first.")).toBeInTheDocument();
    expect(select).toHaveAccessibleDescription("Finish or discard the pending timesheet first.");

    answer.current = 404;
    await user.click(screen.getByRole("button", { name: "Prepare payroll" }));
    expect(await screen.findByText("No timesheet hours for this milestone yet.")).toBeInTheDocument();
    expect(toasts).toEqual([]);
    off();
  });

  it("a pending mapping blocks Prepare, and the mock agrees with the server", async () => {
    const user = userEvent.setup();
    renderPayroll();
    await user.upload(screen.getByLabelText("Timesheet file"), csv());
    const dialog = await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    expect(dialog).not.toBeInTheDocument();
    expect(screen.getByText("This timesheet is waiting for you to match its columns.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare payroll" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Match columns" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("runs list renders", async () => {
    renderPayroll();
    const grid = await screen.findByRole("region", { name: "Payroll runs" });
    expect(await within(grid).findByText("Needs your approval")).toBeInTheDocument();
    expect(within(grid).getByText("MS-01")).toBeInTheDocument();
    expect(within(grid).getByRole("link", { name: /Open run / })).toHaveAttribute("href", expect.stringMatching(/^\/leash\/payroll\/.+/));
    expect(within(grid).getByText("Not sent")).toBeInTheDocument();
  });
});
