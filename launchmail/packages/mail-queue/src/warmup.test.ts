import { describe, expect, it } from "vitest";
import { warmupDailyCap, dayKey, WARMUP_DAILY_CAPS } from "./warmup";

describe("warmupDailyCap", () => {
  it("ramps the daily cap by sender age", () => {
    expect(warmupDailyCap(0)).toBe(WARMUP_DAILY_CAPS[0]);
    expect(warmupDailyCap(1)).toBe(WARMUP_DAILY_CAPS[1]);
    expect(warmupDailyCap(0)).toBeLessThan(warmupDailyCap(2));
  });
  it("clamps a negative age to day 0", () => {
    expect(warmupDailyCap(-5)).toBe(WARMUP_DAILY_CAPS[0]);
  });
  it("graduates to unlimited past the ladder", () => {
    expect(warmupDailyCap(WARMUP_DAILY_CAPS.length)).toBe(Infinity);
    expect(warmupDailyCap(9999)).toBe(Infinity);
  });
});

describe("dayKey", () => {
  it("buckets by UTC day and lowercases the domain", () => {
    const t = 5 * 86_400_000 + 12_345; // day 5
    expect(dayKey("Ripieno.XYZ", t)).toBe("warmup:day:ripieno.xyz:5");
  });
  it("rolls to the next key at the day boundary", () => {
    const a = dayKey("x.io", 5 * 86_400_000 + 86_399_000);
    const b = dayKey("x.io", 6 * 86_400_000);
    expect(a).not.toBe(b);
  });
});
