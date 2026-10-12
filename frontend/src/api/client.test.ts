import { describe, expect, it, vi } from "vitest";
import { api, ApiError, jobPolling } from "./client";
import { mockApi } from "../test/helpers";

describe("api client", () => {
  it("repeats array parameters and leaves out empty ones", async () => {
    const { calls } = mockApi({
      "GET /api/profiles/p/periods/2026-01/cases": { body: { total: 0, items: [] } },
    });

    await api.cases("p", "2026-01", { status: ["open", "fix_reported"], kind: [], q: "", sort: "amount", limit: 25, offset: 0 });

    expect(calls[0]?.path).toBe("/api/profiles/p/periods/2026-01/cases?status=open&status=fix_reported&sort=amount&limit=25&offset=0");
  });

  it("turns the server's error shape into an ApiError", async () => {
    mockApi({
      "POST /api/cases/c/report-fix": {
        status: 409,
        body: { error: { code: "stale_case", message: "case is at version 5", current_version: 5 } },
      },
    });

    const error = await api.reportFix("c", 3, "key").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "stale_case", currentVersion: 5, message: "case is at version 5" });
  });

  it("reports an unreadable error body without crashing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })));

    await expect(api.profiles()).rejects.toMatchObject({ status: 502, code: "http_error" });
  });

  it("reports a network failure in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("failed"))));

    await expect(api.profiles()).rejects.toMatchObject({ status: 0, code: "network" });
  });

  it("sends the idempotency key and the version the user saw", async () => {
    const { calls } = mockApi({ "POST /api/cases/c/report-fix": { body: {} } });

    await api.reportFix("c", 4, "abc-123");

    expect(calls[0]?.headers["Idempotency-Key"]).toBe("abc-123");
    expect(calls[0]?.body).toEqual({ expected_version: 4 });
  });

  describe("queued reconciliation", () => {
    const run = { id: "r1", status: "succeeded", mode: "full", partitions_total: 1, partitions_recomputed: 1, finished_at: null };
    const cases = { applied: true, opened: 3, updated: 0, reopened: 0, resolved: 0, withdrawn: 0, fix_not_verified: 0 };
    const job = (status: string, extra: Record<string, unknown> = {}) => ({
      id: "j1",
      kind: "reconcile_period",
      status,
      attempts: 1,
      max_attempts: 5,
      last_error: null,
      created_at: "2026-10-12T10:00:00Z",
      finished_at: null,
      run: null,
      cases: null,
      ...extra,
    });
    const file = { version_number: 2 };
    const upload = (queued: unknown) => ({ file, created: true, reconciled: false, run: null, cases: null, job: queued });

    it("waits for the job and returns the result the caller would have got", async () => {
      jobPolling.intervalMs = 0;
      const states = [job("running"), job("succeeded", { run, cases })];
      const { calls } = mockApi({
        "POST /api/demo/load-corrected-export": { status: 202, body: upload(job("queued")) },
        "GET /api/jobs/j1": () => ({ body: states.shift() }),
      });

      const result = await api.loadCorrectedExport();

      expect(calls.filter((c) => c.path === "/api/jobs/j1")).toHaveLength(2);
      expect(result.cases).toMatchObject({ applied: true, opened: 3 });
      expect(result.run?.status).toBe("succeeded");
      expect(result.reconciled).toBe(true);
    });

    it("does not poll when the server did the work in the request", async () => {
      const { calls } = mockApi({
        "POST /api/demo/load-corrected-export": {
          body: { file, created: true, reconciled: true, run, cases, job: null },
        },
      });

      const result = await api.loadCorrectedExport();

      expect(calls).toHaveLength(1);
      expect(result.cases?.opened).toBe(3);
    });

    it("turns a failed job into an error that says why", async () => {
      jobPolling.intervalMs = 0;
      mockApi({
        "POST /api/demo/load-corrected-export": { status: 202, body: upload(job("queued")) },
        "GET /api/jobs/j1": { body: job("failed", { last_error: "ValueError: bad period" }) },
      });

      await expect(api.loadCorrectedExport()).rejects.toMatchObject({
        code: "job_failed",
        message: "Reconciliation failed: ValueError: bad period",
      });
    });

    it("gives up waiting after the time allowed", async () => {
      jobPolling.intervalMs = 0;
      jobPolling.timeoutMs = -1;
      mockApi({
        "POST /api/demo/load-corrected-export": { status: 202, body: upload(job("queued")) },
      });

      await expect(api.loadCorrectedExport()).rejects.toMatchObject({ code: "job_timeout" });
      jobPolling.timeoutMs = 10 * 60 * 1000;
    });

    it("waits on a confirmed setup too", async () => {
      jobPolling.intervalMs = 0;
      mockApi({
        "POST /api/setup/proposals/p1/confirm": {
          status: 202,
          body: { profile: { id: "x", name: "n", reconciliation_type: "invoice_payment" }, period: "2026-01", created: true, records: {}, rejected: {}, run: null, cases: null, job: job("queued") },
        },
        "GET /api/jobs/j1": { body: job("succeeded", { run, cases }) },
      });

      const result = await api.confirmMapping("p1", { name: "n", invoices: {} as never, payments: {} as never });

      expect(result.cases?.opened).toBe(3);
    });
  });
});
