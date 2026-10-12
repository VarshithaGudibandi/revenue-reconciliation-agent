import { describe, expect, it } from "vitest";
import { formatCount, formatDate, formatPercent, formatPeriod, newIdempotencyKey } from "./formatters";

describe("formatters", () => {
  it("formats a calendar date without shifting it by time zone", () => {
    expect(formatDate("2026-01-05")).toBe("Jan 5, 2026");
    expect(formatDate("2026-12-31")).toBe("Dec 31, 2026");
  });

  it("formats a period as a month", () => {
    expect(formatPeriod("2026-01")).toBe("January 2026");
    expect(formatPeriod("nonsense")).toBe("nonsense");
  });

  it("formats a rate as a percentage with one decimal, and a missing one as a dash", () => {
    expect(formatPercent(0.905)).toBe("90.5%");
    expect(formatPercent(1)).toBe("100.0%");
    expect(formatPercent(null)).toBe("–");
  });

  it("groups thousands", () => {
    expect(formatCount(1234567)).toBe("1,234,567");
  });

  it("makes a different idempotency key for each action", () => {
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
