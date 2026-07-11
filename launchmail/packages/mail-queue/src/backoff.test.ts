import { describe, expect, it } from "vitest";
import {
  MAIL_RETRY_SCHEDULE_MS,
  MAIL_JOB_ATTEMPTS,
  mailBackoffDelay,
} from "./backoff";

describe("mail backoff schedule", () => {
  it("attempts = schedule length + 1 (initial send + one per retry slot)", () => {
    expect(MAIL_JOB_ATTEMPTS).toBe(MAIL_RETRY_SCHEDULE_MS.length + 1);
  });

  it("returns the greylisting-friendly ladder by attempt", () => {
    expect(mailBackoffDelay(1)).toBe(60_000); // +1 min
    expect(mailBackoffDelay(2)).toBe(5 * 60_000); // +5 min
    expect(mailBackoffDelay(4)).toBe(60 * 60_000); // +1 h
  });

  it("clamps past the last slot to the final (24h) delay", () => {
    const last = MAIL_RETRY_SCHEDULE_MS[MAIL_RETRY_SCHEDULE_MS.length - 1];
    expect(mailBackoffDelay(999)).toBe(last);
  });

  it("is monotonically non-decreasing", () => {
    for (let i = 2; i <= MAIL_RETRY_SCHEDULE_MS.length; i++) {
      expect(mailBackoffDelay(i)).toBeGreaterThanOrEqual(mailBackoffDelay(i - 1));
    }
  });
});
