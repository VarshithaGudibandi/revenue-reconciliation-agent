import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { caseDetail, mockApi, renderWithClient } from "../test/helpers";
import { CaseDrawer } from "./CaseDrawer";

/** A server whose case changes once an action succeeds, so the refetch after it agrees. */
function open(overrides: Record<string, unknown> = {}, action?: { path: string; result: unknown }) {
  let current: unknown = caseDetail(overrides);
  const api = mockApi({
    "GET /api/cases/case-1": () => ({ body: current }),
    ...(action
      ? {
          [`POST ${action.path}`]: () => {
            current = action.result;
            return { body: current };
          },
        }
      : {}),
  });
  renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);
  return api;
}

describe("CaseDrawer", () => {
  it("explains the case from its records", async () => {
    open();

    expect(await screen.findByRole("heading", { name: "Invoice INV-1 is paid 100.00 USD short." })).toBeInTheDocument();
    expect(screen.getByText(/payments total 900.00 USD/)).toBeInTheDocument();
    expect(screen.getByText("Check for a fee or credit note.")).toBeInTheDocument();
    expect(screen.getByText(/Written by rules from the records, not generated/)).toBeInTheDocument();
    expect(screen.getByText("INV-1")).toBeInTheDocument();
    expect(screen.getByText("PAY-1")).toBeInTheDocument();
  });

  it("offers the actions that make sense for an open case, and no others", async () => {
    open();

    expect(await screen.findByRole("button", { name: "I fixed this in the source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept this difference…" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Take back/ })).not.toBeInTheDocument();
  });

  it("reports a fix with the version it saw and an idempotency key, and shows the result", async () => {
    const updated = caseDetail({ status: "fix_reported", version: 4 });
    const { calls } = open({}, { path: "/api/cases/case-1/report-fix", result: updated });

    await userEvent.click(await screen.findByRole("button", { name: "I fixed this in the source" }));

    expect(await screen.findByText(/You reported this as fixed/)).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ expected_version: 3 });
    expect(post.headers["Idempotency-Key"]).toMatch(/\S{8,}/);
    expect(screen.getByRole("button", { name: /Take back the fix report/ })).toBeInTheDocument();
  });

  it("explains a stale case in plain words and refetches it", async () => {
    let reads = 0;
    mockApi({
      "GET /api/cases/case-1": () => ({ body: caseDetail({ version: ++reads === 1 ? 3 : 5 }) }),
      "POST /api/cases/case-1/report-fix": {
        status: 409,
        body: { error: { code: "stale_case", message: "case is at version 5", current_version: 5 } },
      },
    });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

    await userEvent.click(await screen.findByRole("button", { name: "I fixed this in the source" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed since you opened it, so nothing was changed/);
    await waitFor(() => expect(reads).toBeGreaterThan(1));
  });

  it("will not accept a difference without a note", async () => {
    const { calls } = open();

    await userEvent.click(await screen.findByRole("button", { name: "Accept this difference…" }));
    const dialog = screen.getByRole("dialog", { name: "Accept this difference" });
    const submit = within(dialog).getByRole("button", { name: "Accept difference" });
    expect(submit).toBeDisabled();

    await userEvent.type(within(dialog).getByLabelText("Note (required)"), "   ");
    expect(submit).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("accepts a difference with a reason and a note", async () => {
    const accepted = caseDetail({ status: "accepted", accepted_reason: "write_off", version: 4 });
    const { calls } = open({}, { path: "/api/cases/case-1/accept", result: accepted });

    await userEvent.click(await screen.findByRole("button", { name: "Accept this difference…" }));
    const dialog = screen.getByRole("dialog", { name: "Accept this difference" });
    await userEvent.selectOptions(within(dialog).getByLabelText("Reason"), "write_off");
    await userEvent.type(within(dialog).getByLabelText("Note (required)"), "Customer refunded");
    await userEvent.click(within(dialog).getByRole("button", { name: "Accept difference" }));

    expect(await screen.findByText(/Accepted as write-off/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      reason: "write_off",
      note: "Customer refunded",
      expected_version: 3,
    });
  });

  it("tells the user when a reported fix did not hold", async () => {
    open({ fix_unverified: true });

    expect(await screen.findByText(/latest data still shows this problem/)).toBeInTheDocument();
    expect(screen.getByText("Open · fix not confirmed")).toBeInTheDocument();
  });

  it("shows that the data resolved a case, and offers no actions on it", async () => {
    open({ status: "resolved", resolved_at: "2026-10-12T12:00:00Z" });

    expect(await screen.findByText(/Resolved by the data/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /fixed this|Accept|Reopen/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add note" })).toBeDisabled();
  });

  it("points a merged case to the case it became part of", async () => {
    const opened: string[] = [];
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail({ status: "withdrawn", closed_reason: "merged", successor_case_id: "case-9" }) },
    });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={(id) => opened.push(id)} />);

    await userEvent.click(await screen.findByRole("button", { name: "Open it" }));

    expect(opened).toEqual(["case-9"]);
  });

  it("adds a note and clears the box", async () => {
    const withNote = caseDetail({ notes: [{ id: 1, body: "Asked billing", created_at: "2026-10-12T11:00:00Z" }] });
    const { calls } = open({}, { path: "/api/cases/case-1/notes", result: withNote });

    const box = await screen.findByLabelText("Add a note");
    await userEvent.type(box, "Asked billing");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));

    expect(await screen.findByText("Asked billing")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ body: "Asked billing" });
    expect(box).toHaveValue("");
  });

  it("says when the case cannot be loaded", async () => {
    mockApi({ "GET /api/cases/case-1": { status: 404, body: { error: { code: "not_found", message: "no case case-1" } } } });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("no case case-1");
  });
});
