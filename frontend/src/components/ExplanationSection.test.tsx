import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { capabilities, caseDetail, explanationFixture, mockApi, renderWithClient } from "../test/helpers";
import { CaseDrawer } from "./CaseDrawer";

const drawer = () => renderWithClient(<CaseDrawer caseId="case-1" onClose={() => {}} onOpenCase={() => {}} />);

describe("ExplanationSection", () => {
  it("is absent when no model is configured and none has written one", async () => {
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail() },
      "GET /api/capabilities": { body: capabilities() },
    });
    drawer();

    await screen.findByText("Why");
    expect(screen.queryByText("In the model's words")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain with the model" })).not.toBeInTheDocument();
  });

  it("offers to explain, and says what it sends", async () => {
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail() },
      "GET /api/capabilities": { body: capabilities("claude-opus-5-5") },
    });
    drawer();

    expect(await screen.findByRole("button", { name: "Explain with the model" })).toBeInTheDocument();
    expect(screen.getByText(/Not the customer or the ids/)).toBeInTheDocument();
  });

  it("shows the checked text and who wrote it, and the rule-written reading stays", async () => {
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail({ explanation: explanationFixture() }) },
      "GET /api/capabilities": { body: capabilities("claude-opus-5-5") },
    });
    drawer();

    expect(await screen.findByText("Checked")).toBeInTheDocument();
    expect(screen.getByText(/Written by claude-opus-5-5/)).toBeInTheDocument();
    expect(screen.getByText(/Ask whether a credit note covers/)).toBeInTheDocument();
    expect(screen.getByText("The invoice is 1,000.00 USD and payments total 900.00 USD.")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was changed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain with the model" })).not.toBeInTheDocument();
  });

  it("asks once and shows what comes back", async () => {
    let current: unknown = caseDetail();
    const { calls } = mockApi({
      "GET /api/cases/case-1": () => ({ body: current }),
      "GET /api/capabilities": { body: capabilities("claude-opus-5-5") },
      "POST /api/cases/case-1/explanation": () => {
        current = caseDetail({ explanation: explanationFixture() });
        return { body: { explanation: explanationFixture(), problems: [] } };
      },
    });
    drawer();

    await userEvent.click(await screen.findByRole("button", { name: "Explain with the model" }));

    expect(await screen.findByText("Checked")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Explain with the model" })).not.toBeInTheDocument());
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("says why a text was not used and leaves the button to try again", async () => {
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail() },
      "GET /api/capabilities": { body: capabilities("claude-opus-5-5") },
      "POST /api/cases/case-1/explanation": {
        body: { explanation: null, problems: ["it states 999.99, which is not one of the engine's figures"] },
      },
    });
    drawer();

    await userEvent.click(await screen.findByRole("button", { name: "Explain with the model" }));

    expect(await screen.findByText(/failed the check, so it is not shown/)).toBeInTheDocument();
    expect(screen.getByText(/999\.99/)).toBeInTheDocument();
    expect(screen.queryByText("Checked")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain with the model" })).toBeEnabled();
    expect(screen.getByText("The invoice is 1,000.00 USD and payments total 900.00 USD.")).toBeInTheDocument();
  });

  it("shows an error from the server without losing the page", async () => {
    mockApi({
      "GET /api/cases/case-1": { body: caseDetail() },
      "GET /api/capabilities": { body: capabilities("claude-opus-5-5") },
      "POST /api/cases/case-1/explanation": {
        status: 409,
        body: { error: { code: "not_applicable", message: "the case has no records to explain" } },
      },
    });
    drawer();

    await userEvent.click(await screen.findByRole("button", { name: "Explain with the model" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("no records to explain");
    expect(screen.getByText("Why")).toBeInTheDocument();
  });
});
