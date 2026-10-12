import { describe, expect, it } from "vitest";
import { describeDateFormat } from "./labels";

describe("describeDateFormat", () => {
  it("says the order of the parts and shows an example", () => {
    expect(describeDateFormat("%d/%m/%Y")).toBe("day-month-year: 31/01/2026");
    expect(describeDateFormat("%m/%d/%Y")).toBe("month-day-year: 01/31/2026");
    expect(describeDateFormat("%Y%m%d")).toBe("year-month-day: 20260131");
    expect(describeDateFormat("%d-%b-%Y")).toBe("day-month-year: 31-Jan-2026");
  });

  it("keeps a time of day out of the order but in the example", () => {
    expect(describeDateFormat("%Y-%m-%d %H:%M:%S")).toBe("year-month-day: 2026-01-31 14:05:09");
  });
});
