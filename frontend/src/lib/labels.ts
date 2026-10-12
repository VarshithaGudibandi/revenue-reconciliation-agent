export const KIND_LABELS: Record<string, string> = {
  missing_payment: "Missing payment",
  unmatched_payment: "Unmatched payment",
  amount_mismatch: "Amount mismatch",
  duplicate_payment: "Duplicate payment",
  partial_payment: "Partial payment",
  misreferenced_payment: "Wrong reference",
  currency_mismatch: "Currency mismatch",
  ambiguous_payment: "Ambiguous payment",
};

export const STATUS_LABELS: Record<string, string> = {
  open: "Open",
  fix_reported: "Fix reported",
  accepted: "Accepted",
  resolved: "Resolved",
  withdrawn: "Withdrawn",
};

export const REASON_LABELS: Record<string, string> = {
  timing_difference: "Timing difference",
  rounding_difference: "Rounding difference",
  write_off: "Write-off",
  known_data_issue: "Known data issue",
  other: "Other",
};

export const EVENT_LABELS: Record<string, string> = {
  opened: "Opened",
  updated: "Updated by new data",
  fix_reported: "Fix reported",
  fix_retracted: "Fix retracted",
  fix_not_verified: "Fix not confirmed by the data",
  accepted: "Variance accepted",
  reopened_by_user: "Reopened",
  reopened_data_changed: "Reopened: the data changed",
  reopened: "Reopened: the problem came back",
  resolved: "Resolved by the data",
  withdrawn: "Withdrawn",
};

export const label = (map: Record<string, string>, key: string): string =>
  map[key] ?? key.replace(/_/g, " ");

export const FIELD_LABELS: Record<"invoices" | "payments", Record<string, string>> = {
  invoices: {
    record_id: "Invoice number",
    counterparty: "Customer",
    date: "Invoice date",
    amount: "Invoice total",
    currency: "Currency",
  },
  payments: {
    record_id: "Payment id",
    counterparty: "Customer",
    date: "Payment date",
    amount: "Amount paid",
    currency: "Currency",
    reference: "Invoice reference",
  },
};

export const FIELD_HINTS: Record<string, string> = {
  record_id: "Unique on every row.",
  counterparty: "The same customer value appears in both files.",
  amount: "Including tax: the amount that gets paid.",
  reference: "The text that names the invoice a payment is for. Leave empty if there is none.",
};

export const DECIMAL_LABELS: Record<string, string> = {
  ".": "Point, as in 1,234.56",
  ",": "Comma, as in 1.234,56",
};

const SAMPLE_PARTS: Record<string, string> = {
  "%Y": "2026",
  "%m": "01",
  "%d": "31",
  "%b": "Jan",
  "%H": "14",
  "%M": "05",
  "%S": "09",
};

/** A date format as a person reads it: the order of its parts and an example. */
export function describeDateFormat(format: string): string {
  const order = [...format.matchAll(/%[YmdbHMS]/g)]
    .map((m) => ({ "%Y": "year", "%m": "month", "%d": "day", "%b": "month", "%H": "hour", "%M": "minute", "%S": "second" })[m[0]])
    .filter((part, index, all) => part && all.indexOf(part) === index)
    .slice(0, 3)
    .join("-");
  const example = format.replace(/%[YmdbHMS]/g, (token) => SAMPLE_PARTS[token] ?? token);
  return `${order}: ${example}`;
}
