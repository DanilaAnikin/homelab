import { describe, expect, it } from "vitest";
import { limitForDomain, windowKey } from "./rate-limiter";

describe("limitForDomain", () => {
  it("applies conservative caps to strict providers (case-insensitive)", () => {
    expect(limitForDomain("gmail.com")).toBe(20);
    expect(limitForDomain("GMAIL.COM")).toBe(20);
    expect(limitForDomain("outlook.com")).toBe(15);
    expect(limitForDomain("seznam.cz")).toBe(30);
  });
  it("falls back to the default cap for unknown domains", () => {
    expect(limitForDomain("some-random-corp.io")).toBe(60);
  });
});

describe("windowKey", () => {
  it("buckets timestamps into fixed 60s windows", () => {
    const base = 60_000 * 1000; // aligned to a window boundary
    const a = windowKey("gmail.com", base);
    const b = windowKey("gmail.com", base + 59_999);
    const c = windowKey("gmail.com", base + 60_000);
    expect(a).toBe(b); // same window
    expect(a).not.toBe(c); // next window
    expect(a).toMatch(/^rl:dom:gmail\.com:\d+$/);
  });
  it("lowercases the domain in the key", () => {
    expect(windowKey("Gmail.COM", 0)).toBe(windowKey("gmail.com", 0));
  });
});
