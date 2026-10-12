import { createContext, useContext } from "react";
import type { PeriodSummary } from "../api/client";

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
