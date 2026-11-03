import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InvestigationSection } from "./InvestigationSection";
import { mockApi, renderWithClient } from "../test/helpers";

function investigation(overrides: Record<string, unknown> = {}) {
  return {
    id: "inv-1",
    case_id: "case-1",
    status: "awaiting_decision",
    model: "gemini-3.1-flash-lite",
    finding: {
      cause: "processor_fee",
      confidence: "high",
      evidence: ["INV-000013", "PAY-000012"],
      action: "accept_variance",
      note: "The 8.99 USD short is 3.13% of INV-000013, a card processor's fee.",
    },
    problems: [],
    trace: [{ tool: "calculate", arguments: { expression: "286.77 - 277.78" }, result: { result: 8.99 } }],
    error: null,
    outcome: null,
    decision_note: null,
    created_at: "2026-11-03T10:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

const section = (canInvestigate = true, kind = "amount_mismatch") => (
  <InvestigationSection caseId="case-1" kind={kind} caseVersion={3} canInvestigate={canInvestigate} />
);

describe("InvestigationSection", () => {
  it("is not offered without a model, or for a kind it cannot investigate", async () => {
    mockApi({ "GET /api/cases/case-1/investigation": { body: null } });
    const { container, unmount } = renderWithClient(section(false));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    unmount();
    const other = renderWithClient(section(true, "duplicate_payment"));
    expect(other.container).toBeEmptyDOMElement();
  });

  it("investigates on request and shows the checked finding with what it looked at", async () => {
    mockApi({
      "GET /api/cases/case-1/investigation": { body: null },
      "POST /api/cases/case-1/investigation": { body: investigation() },
    });
    renderWithClient(section());
    fireEvent.click(await screen.findByRole("button", { name: "Investigate with the model" }));
    expect(await screen.findByText("processor fee")).toBeInTheDocument();
    expect(screen.getByText(/Rests on: INV-000013, PAY-000012/)).toBeInTheDocument();
    expect(screen.getByText(/Accept the variance as a cost/)).toBeInTheDocument();
    expect(screen.getByText(/calculate \(expression 286.77 - 277.78\)/)).toBeInTheDocument();
  });

  it("sends the decision with the person's note and the case version, then says what was done", async () => {
    let sent: unknown = null;
    mockApi({
      "GET /api/cases/case-1/investigation": { body: investigation() },
      "POST /api/investigations/inv-1/decision": (init?: RequestInit) => {
        sent = JSON.parse(String(init?.body));
        return { body: investigation({ status: "applied", outcome: "accepted", decided_at: "2026-11-03T10:05:00Z" }) };
      },
    });
    renderWithClient(section());
    fireEvent.change(await screen.findByRole("textbox"), { target: { value: "agreed with finance" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/the variance was accepted/)).toBeInTheDocument();
    expect(sent).toEqual({ approve: true, note: "agreed with finance", expected_version: 3 });
  });

  it("says why a finding that failed the checks is not shown, and offers no approval", async () => {
    mockApi({
      "GET /api/cases/case-1/investigation": {
        body: investigation({ status: "not_shown", problems: ["it states 98765.43, which no tool showed"] }),
      },
    });
    renderWithClient(section());
    expect(await screen.findByText(/98765.43, which no tool showed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
