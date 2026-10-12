import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Suggestion } from "../api/client";
import { capabilities, caseDetail, mockApi, renderWithClient, suggestionFixture } from "../test/helpers";
import { CaseDrawer } from "./CaseDrawer";
import { SuggestionSection } from "./SuggestionSection";

const view = (suggestion: Record<string, unknown>, canAskModel = false) =>
  renderWithClient(
    <SuggestionSection
      caseId="case-1"
      suggestion={suggestion as unknown as Suggestion}
      canAskModel={canAskModel}
    />,
  );

describe("SuggestionSection", () => {
  it("names the invoice and quotes the memo when the rules are confident", () => {
    view(suggestionFixture({ evidence: "inv 121" }));

    expect(screen.getByText("INV-000121")).toBeInTheDocument();
    expect(screen.getByText("Confident")).toBeInTheDocument();
    expect(screen.getByText(/“inv 121”/)).toBeInTheDocument();
    expect(screen.getByText(/Read by rules\./)).toBeInTheDocument();
  });

  it("says plainly that it is advice and nothing was changed", () => {
    view(suggestionFixture());

    expect(screen.getByText(/Advice only: nothing was changed/)).toBeInTheDocument();
    expect(screen.getByText(/correct the reference on the payment in the source system/)).toBeInTheDocument();
  });

  it("marks a model's suggestion as not settled, in words, and says who read it", () => {
    view(
      suggestionFixture({
        source: "rules and model",
        model: "claude-opus-5-5",
        confident: false,
        evidence: "cloud servers",
        reasons: ["Suggested by the model; the rules could not settle it"],
      }),
    );

    expect(screen.getByText("A suggestion, not settled")).toBeInTheDocument();
    expect(screen.queryByText("Confident")).not.toBeInTheDocument();
    expect(screen.getByText(/Read by rules and claude-opus-5-5/)).toBeInTheDocument();
    expect(screen.getByText(/Suggested by the model/)).toBeInTheDocument();
  });

  it("says when nothing in the memo separates the invoices, and lists those still in the running", () => {
    view(
      suggestionFixture({
        invoice_id: null,
        confident: false,
        evidence: null,
        alternatives: ["INV-000120", "INV-000121"],
        reasons: ["The rules and the model disagree"],
      }),
    );

    expect(screen.getByText(/Nothing in the payment's memo tells these invoices apart/)).toBeInTheDocument();
    expect(screen.getByText("INV-000120, INV-000121")).toBeInTheDocument();
    expect(screen.getByText(/disagree/)).toBeInTheDocument();
  });

  it("offers the model only when one is configured and has not answered, and says what it sends", () => {
    const { unmount } = view(suggestionFixture({ invoice_id: null, confident: false }), false);
    expect(screen.queryByRole("button", { name: "Ask the model" })).not.toBeInTheDocument();
    unmount();

    const second = view(suggestionFixture({ invoice_id: null, confident: false }), true);
    expect(screen.getByRole("button", { name: "Ask the model" })).toBeInTheDocument();
    expect(screen.getByText(/Not the customer or the amounts/)).toBeInTheDocument();
    second.unmount();

    view(suggestionFixture({ source: "rules and model", model: "m" }), true);
    expect(screen.queryByRole("button", { name: "Ask the model" })).not.toBeInTheDocument();
  });

  it("asks once and shows the answer that comes back", async () => {
    const withModel = suggestionFixture({
      source: "rules and model",
      model: "claude-opus-5-5",
      confident: false,
      evidence: "Beratung",
    });
    let current: unknown = caseDetail({
      kind: "ambiguous_payment",
      suggestion: suggestionFixture({ invoice_id: null, confident: false, evidence: null }),
    });
    const { calls } = mockApi({
      "GET /api/cases/case-1": () => ({ body: current }),
      "GET /api/capabilities": { body: capabilities("m") },
      "POST /api/cases/case-1/suggestion": () => {
        current = caseDetail({ kind: "ambiguous_payment", suggestion: withModel });
        return { body: withModel };
      },
    });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

    await userEvent.click(await screen.findByRole("button", { name: "Ask the model" }));

    expect(await screen.findByText(/“Beratung”/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Ask the model" })).not.toBeInTheDocument());
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("shows why asking failed without losing the suggestion", async () => {
    mockApi({
      "GET /api/cases/case-1": {
        body: caseDetail({
          kind: "ambiguous_payment",
          suggestion: suggestionFixture({ invoice_id: null, confident: false }),
        }),
      },
      "GET /api/capabilities": { body: capabilities("m") },
      "POST /api/cases/case-1/suggestion": {
        status: 409,
        body: { error: { code: "not_applicable", message: "only an ambiguous payment can be asked about" } },
      },
    });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

    await userEvent.click(await screen.findByRole("button", { name: "Ask the model" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("only an ambiguous payment");
    expect(screen.getByText(/Nothing in the payment's memo/)).toBeInTheDocument();
  });

  it("is absent from a case that has no suggestion, and the drawer shows the text of each record", async () => {
    mockApi({
      "GET /api/cases/case-1": {
        body: caseDetail({
          records: [
            {
              role: "payment",
              record_id: "PAY-1",
              customer: "CUST-1",
              occurred_on: "2026-01-12",
              amount: { minor: 100, currency: "USD", display: "1.00 USD" },
              in_current_files: true,
              text: "pls apply to inv 121",
            },
          ],
        }),
      },
      "GET /api/capabilities": { body: capabilities() },
    });
    renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

    expect(await screen.findByText("pls apply to inv 121")).toBeInTheDocument();
    expect(screen.queryByText("Which invoice was meant?")).not.toBeInTheDocument();
  });
});
