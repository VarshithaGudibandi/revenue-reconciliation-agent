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
