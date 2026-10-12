import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PeriodSummary } from "../api/client";
import { CaseTable } from "../components/CaseTable";
import { Hero } from "../components/Hero";
import { MatchTable } from "../components/MatchTable";
import { INVOICE_LABELS, LabelsProvider, dateHeading, settledHeading, type TypeLabels } from "./typeLabels";

const DEPOSIT_LABELS: TypeLabels = {
  type: "payout_deposit",
  title: "Bank deposits and processor payouts",
  left: "Deposit",
  left_plural: "deposits",
  right: "Payout",
  right_plural: "payouts",
};

const summary = (labels: TypeLabels) =>
  ({
    labels,
    invoices: 150,
    payments: 400,
    matched_invoices: 120,
    reconciliation_rate: 0.8,
  }) as unknown as PeriodSummary;

const caseRow = {
  id: "c1",
  number: 3,
  status: "open",
  kind: "deposit_mismatch",
  summary: "Deposit BNK-000012 is 30.00 USD short of its payouts.",
  customer: "STRIPE",
  variance: { minor: -3000, currency: "USD", display: "-30.00 USD" },
  invoice_ids: ["BNK-000012"],
  payment_ids: ["PO-000020", "PO-000021"],
  proposed_invoice_id: null,
  fix_unverified: false,
  accepted_reason: null,
  version: 1,
  updated_at: "2026-10-12T10:00:00Z",
};

describe("labels for a type of reconciliation", () => {
  it("leads with deposits and payouts when that is what is reconciled", () => {
    render(<Hero summary={summary(DEPOSIT_LABELS)} />);

    expect(screen.getByText("of 150 deposits reconcile with their payouts")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Deposits reconciled" })).toBeInTheDocument();
  });

  it("still says invoices and payments for the original type", () => {
    render(<Hero summary={summary(INVOICE_LABELS)} />);

    expect(screen.getByText("of 150 invoices reconcile with their payments")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Invoices reconciled" })).toBeInTheDocument();
  });

  it("names the columns of the case table after the two sides", () => {
    render(
      <LabelsProvider value={DEPOSIT_LABELS}>
        <CaseTable rows={[caseRow] as never} selectedId={null} onOpen={() => {}} />
      </LabelsProvider>,
    );

    expect(screen.getByRole("columnheader", { name: "Deposits" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Payouts" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Invoices" })).not.toBeInTheDocument();
    expect(screen.getByText("BNK-000012")).toBeInTheDocument();
  });

  it("uses the invoice wording in the case table by default", () => {
    render(<CaseTable rows={[caseRow] as never} selectedId={null} onOpen={() => {}} />);

    expect(screen.getByRole("columnheader", { name: "Invoices" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Payments" })).toBeInTheDocument();
  });

  it("names the matched table's columns, and its empty state, after the two sides", () => {
    const { rerender } = render(
      <LabelsProvider value={DEPOSIT_LABELS}>
        <MatchTable rows={[]} />
      </LabelsProvider>,
    );
    expect(screen.getByText("No matched deposits to show.")).toBeInTheDocument();

    rerender(
      <LabelsProvider value={DEPOSIT_LABELS}>
        <MatchTable
          rows={[
            {
              invoice_id: "BNK-000012",
              customer: "STRIPE",
              issue_date: "2026-01-20",
              amount: { minor: 100, currency: "USD", display: "1.00 USD" },
              payment_ids: ["PO-000020"],
              paid: { minor: 100, currency: "USD", display: "1.00 USD" },
              variance: { minor: 0, currency: "USD", display: "0.00 USD" },
              rule: "reference",
            } as never,
          ]}
        />
      </LabelsProvider>,
    );
    expect(screen.getByRole("columnheader", { name: "Deposit" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Booked" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Deposit amount" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Payouts" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Payouts total" })).toBeInTheDocument();
  });

  it("keeps the invoice headings for invoices", () => {
    expect(dateHeading(INVOICE_LABELS)).toBe("Issued");
    expect(settledHeading(INVOICE_LABELS)).toBe("Paid");
  });
});
