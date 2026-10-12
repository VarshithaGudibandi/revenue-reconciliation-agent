import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

export type Handler = (init: RequestInit | undefined, url: URL) => unknown | Promise<unknown>;

interface Reply {
  status?: number;
  body: unknown;
}

/** Replace fetch with canned answers keyed by "METHOD /path". Unlisted requests fail the test. */
export function mockApi(routes: Record<string, Reply | ((init: RequestInit | undefined, url: URL) => Reply)>) {
  const calls: { method: string; path: string; headers: Record<string, string>; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const key = `${method} ${url.pathname}`;
    calls.push({
      method,
      path: url.pathname + url.search,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
    });
    const route = routes[key];
    if (!route) throw new Error(`unexpected request: ${key}`);
    const reply = typeof route === "function" ? route(init, url) : route;
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

export const amount = (minor: number, currency = "USD") => ({
  minor,
  currency,
  display: `${(minor / 100).toLocaleString("en-US", { minimumFractionDigits: 2 })} ${currency}`,
});

export function caseDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: "case-1",
    number: 7,
    profile_id: "profile-1",
    period: "2026-01",
    status: "open",
    kind: "amount_mismatch",
    variance: amount(10_000),
    proposed_invoice_id: null,
    fix_unverified: false,
    accepted_reason: null,
    closed_reason: null,
    successor_case_id: null,
    resolved_at: null,
    version: 3,
    created_at: "2026-10-12T10:00:00Z",
    updated_at: "2026-10-12T10:00:00Z",
    analysis: {
      summary: "Invoice INV-1 is paid 100.00 USD short.",
      why: "The invoice is 1,000.00 USD and payments total 900.00 USD.",
      next_steps: ["Check for a fee or credit note."],
    },
    records: [
      { role: "invoice", record_id: "INV-1", customer: "CUST-1", occurred_on: "2026-01-05", amount: amount(100_000), in_current_files: true },
      { role: "payment", record_id: "PAY-1", customer: "CUST-1", occurred_on: "2026-01-12", amount: amount(90_000), in_current_files: true },
    ],
    events: [{ id: 1, kind: "opened", from_status: null, to_status: "open", run_id: null, detail: {}, created_at: "2026-10-12T10:00:00Z" }],
    notes: [],
    ...overrides,
  };
}

function choice(field: string, column: string | null, confident: boolean, extra: Record<string, unknown> = {}) {
  return { field, column, confident, alternatives: [], reasons: [], ...extra };
}

/** A proposal in which everything is settled except the invoice total and the invoice date order. */
export function proposalFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "proposal-1",
    period: "2026-01",
    status: "proposed",
    mapper: "rules",
    model: null,
    profile_id: null,
    date_formats: ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
    invoices: {
      source: "invoices",
      file_name: "invoices.csv",
      rows: 48,
      headers: ["inv", "cust", "dt", "ccy", "net", "gross", "desc"],
      needs_answer: 2,
      fields: [
        choice("record_id", "inv", true, { reasons: ["The only column of unique values"] }),
        choice("counterparty", "cust", true),
        choice("date", "dt", true),
        choice("amount", null, false, {
          alternatives: ["net", "gross"],
          reasons: ["No header word points to this field"],
        }),
        choice("currency", "ccy", true),
        choice("text", "desc", true),
      ],
      date_format: {
        value: null,
        confident: false,
        alternatives: ["%d/%m/%Y", "%m/%d/%Y"],
        reasons: ["Every value fits both day-month and month-day"],
      },
      decimal_separator: { value: ".", confident: true, alternatives: [], reasons: [] },
    },
    payments: {
      source: "payments",
      file_name: "payments.csv",
      rows: 125,
      headers: ["pid", "ref", "cust", "dt", "ccy", "amt"],
      needs_answer: 0,
      fields: [
        choice("record_id", "pid", true),
        choice("counterparty", "cust", true),
        choice("date", "dt", true),
        choice("amount", "amt", true),
        choice("currency", "ccy", true),
        choice("reference", "ref", true),
        choice("text", null, true),
      ],
      date_format: { value: "%m/%d/%Y", confident: true, alternatives: [], reasons: [] },
      decimal_separator: { value: ".", confident: true, alternatives: [], reasons: [] },
    },
    ...overrides,
  };
}

export function previewFixture(ready = true, invoiceError: string | null = null) {
  const sample = (row: number, id: string, reference: string | null = null) => ({
    row,
    record_id: id,
    counterparty: "CUST-0001",
    occurred_on: "2026-01-12",
    amount: amount(42_000),
    reference,
  });
  const file = (source: string, records: number, error: string | null = null) => ({
    source,
    error,
    records_read: error ? 0 : records,
    rejected: 0,
    sample: [sample(1, source === "invoices" ? "INV-000001" : "PAY-000001", source === "payments" ? "INV000001" : null)],
    rejections: [],
  });
  return { invoices: file("invoices", 48, invoiceError), payments: file("payments", 125), ready };
}

export function suggestionFixture(overrides: Record<string, unknown> = {}) {
  return {
    source: "rules",
    model: null,
    invoice_id: "INV-000121",
    confident: true,
    evidence: "121",
    reasons: ["The memo names invoice number 121"],
    alternatives: [],
    ...overrides,
  };
}

export const capabilities = (model: string | null = null) => ({
  mapper: { name: model ? "rules and model" : "rules", model },
  adjudicator: { name: model ? "rules and model" : "rules", model },
  explainer: { name: model ? "model, checked" : "rules", model },
});

export function explanationFixture(overrides: Record<string, unknown> = {}) {
  return {
    model: "claude-opus-5-5",
    summary: "INV-1 is paid 100.00 USD short.",
    why: "PAY-1 paid 900.00 USD against an invoice of 1,000.00 USD.",
    next_steps: ["Ask whether a credit note covers the 100.00 USD."],
    ...overrides,
  };
}
