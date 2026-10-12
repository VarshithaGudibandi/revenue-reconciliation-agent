import { createContext, useContext } from "react";
import type { PeriodSummary } from "../api/client";
import { FIELD_HINTS, FIELD_LABELS } from "./labels";

export type TypeLabels = PeriodSummary["labels"];

/** What the screens say until a period says otherwise: invoices and payments. */
export const INVOICE_LABELS: TypeLabels = {
  type: "invoice_payment",
  title: "Invoices and payments",
  left: "Invoice",
  left_plural: "invoices",
  right: "Payment",
  right_plural: "payments",
};

const Context = createContext<TypeLabels>(INVOICE_LABELS);

export const LabelsProvider = Context.Provider;
export const useTypeLabels = () => useContext(Context);

export const capitalise = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** Column headings that read differently for a deposit and its payouts than for an invoice. */
export function dateHeading(labels: TypeLabels) {
  return labels.type === "invoice_payment" ? "Issued" : "Booked";
}

export function settledHeading(labels: TypeLabels) {
  return labels.type === "invoice_payment" ? "Paid" : `${capitalise(labels.right_plural)} total`;
}

export const DEPOSIT_LABELS: TypeLabels = {
  type: "payout_deposit",
  title: "Bank deposits and processor payouts",
  left: "Deposit",
  left_plural: "deposits",
  right: "Payout",
  right_plural: "payouts",
};

export const TYPE_CHOICES = [INVOICE_LABELS, DEPOSIT_LABELS];

const DEPOSIT_FIELD_LABELS: typeof FIELD_LABELS = {
  invoices: {
    record_id: "Bank transaction id",
    counterparty: "Account",
    date: "Booking date",
    amount: "Deposit amount",
    currency: "Currency",
    text: "Memo",
  },
  payments: {
    record_id: "Payout id",
    counterparty: "Processor",
    date: "Payout date",
    amount: "Payout amount",
    currency: "Currency",
    reference: "Deposit reference",
    text: "Description",
  },
};

export function fieldLabels(labels: TypeLabels): typeof FIELD_LABELS {
  return labels.type === "payout_deposit" ? DEPOSIT_FIELD_LABELS : FIELD_LABELS;
}

export function fieldHints(labels: TypeLabels): Record<string, string> {
  if (labels.type !== "payout_deposit") return FIELD_HINTS;
  return {
    ...FIELD_HINTS,
    counterparty: "The same account or processor value appears in both files.",
    amount: "The amount of this row. A deposit is the sum of its payouts.",
    reference: "The text that names the deposit a payout was paid into. Leave empty if there is none.",
    text: "Free text the source holds about the row. Leave empty if there is none.",
  };
}
