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
