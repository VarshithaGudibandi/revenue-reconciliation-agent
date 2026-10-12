import { describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";
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
});
