import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { FileProposal } from "../api/client";
import { proposalFixture } from "../test/helpers";
import { MappingCard, initialAnswered, initialMapping, isComplete, toBody } from "./MappingCard";

const invoices = proposalFixture().invoices as unknown as FileProposal;
const payments = proposalFixture().payments as unknown as FileProposal;

function renderCard(source: "invoices" | "payments", proposal: FileProposal, extra = {}) {
  const onChange = vi.fn();
  const onAccept = vi.fn();
  const mapping = initialMapping(proposal);
  render(
    <MappingCard
      source={source}
      proposal={proposal}
      dateFormats={["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]}
      mapping={mapping}
      answered={initialAnswered(source, proposal)}
      onChange={onChange}
      onAccept={onAccept}
      {...extra}
    />,
  );
  return { onChange, onAccept };
}

describe("MappingCard", () => {
  it("separates what is confident from what needs an answer, in words and not only in colour", () => {
    renderCard("invoices", invoices);

    const total = screen.getByLabelText("Invoice total").closest("tr")!;
    expect(within(total).getByText("Needs your answer")).toBeInTheDocument();
    const number = screen.getByLabelText("Invoice number").closest("tr")!;
    expect(within(number).getByText("Confident")).toBeInTheDocument();
    expect(within(number).getByText("The only column of unique values")).toBeInTheDocument();
    const format = screen.getByLabelText("Date format").closest("tr")!;
    expect(within(format).getByText("Needs your answer")).toBeInTheDocument();
  });

  it("offers the columns the engine thought possible, marked as such", () => {
    renderCard("invoices", invoices);

    const options = within(screen.getByLabelText("Invoice total")).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toContain("net · possible");
    expect(options.map((o) => o.textContent)).toContain("gross · possible");
  });

  it("offers the date orders the values left open, described for a person", () => {
    renderCard("invoices", invoices);

    const options = within(screen.getByLabelText("Date format")).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(
      expect.arrayContaining(["day-month-year: 31/01/2026", "month-day-year: 01/31/2026"]),
    );
  });

  it("reports a change, and lets a payment file say it has no reference column", async () => {
    const { onChange } = renderCard("payments", payments);

    await userEvent.selectOptions(screen.getByLabelText("Invoice reference"), "No such column");
    await userEvent.selectOptions(screen.getByLabelText("Amount paid"), "pid");

    expect(onChange).toHaveBeenCalledWith("reference", "");
    expect(onChange).toHaveBeenCalledWith("amount", "pid");
  });

  it("shows no reference row for an invoice file", () => {
    renderCard("invoices", invoices);

    expect(screen.queryByLabelText("Invoice reference")).not.toBeInTheDocument();
  });

  it("offers 'This is right' only for a suggestion that is not yet answered", async () => {
    const suggested = {
      ...invoices,
      fields: invoices.fields.map((f) =>
        f.field === "amount" ? { ...f, column: "gross", alternatives: ["net"] } : f,
      ),
    } as FileProposal;
    const { onAccept } = renderCard("invoices", suggested);

    const row = screen.getByLabelText("Invoice total").closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "This is right" }));

    expect(onAccept).toHaveBeenCalledWith("amount");
    expect(within(screen.getByLabelText("Invoice number").closest("tr")!).queryByRole("button")).toBeNull();
  });

  it("drops the 'possible' mark once the column is the chosen one", () => {
    const chosen = {
      ...invoices,
      fields: invoices.fields.map((f) => (f.field === "amount" ? { ...f, column: "gross" } : f)),
    } as FileProposal;
    renderCard("invoices", chosen);

    const options = within(screen.getByLabelText("Invoice total")).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toContain("gross");
    expect(options.map((o) => o.textContent)).toContain("net · possible");
  });
});

describe("mapping state", () => {
  it("starts from what was proposed and treats only the confident rows as answered", () => {
    const mapping = initialMapping(invoices);
    const answered = initialAnswered("invoices", invoices);

    expect(mapping.record_id).toBe("inv");
    expect(mapping.amount).toBe("");
    expect(mapping.date_format).toBe("");
    expect(answered.has("invoices.record_id")).toBe(true);
    expect(answered.has("invoices.amount")).toBe(false);
    expect(answered.has("invoices.date_format")).toBe(false);
    expect(answered.has("invoices.decimal_separator")).toBe(true);
  });

  it("can only be previewed once everything required is chosen, and sends no reference as null", () => {
    const mapping = initialMapping(payments);
    expect(isComplete(initialMapping(invoices))).toBe(false);
    expect(isComplete(mapping)).toBe(true);

    expect(toBody({ ...mapping, reference: "" }).reference).toBeNull();
    expect(toBody(mapping).reference).toBe("ref");
    expect(toBody(mapping).decimal_separator).toBe(".");
  });
});
