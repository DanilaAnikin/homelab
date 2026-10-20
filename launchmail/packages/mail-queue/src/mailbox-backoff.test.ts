import { describe, expect, it } from "vitest";
import { MailboxBackoff } from "./mailbox-backoff";

function clock(start = 1_000_000) {
  let now = start;
  return {
    now: () => now,
    advance: (ms: number) => {
      now += ms;
    },
  };
}

describe("MailboxBackoff", () => {
  it("doubles the delay per consecutive failure up to the cap", () => {
    const c = clock();
    const backoff = new MailboxBackoff({
      baseMs: 30_000,
      maxMs: 600_000,
      jitter: 0,
      now: c.now,
    });
    const delays = Array.from({ length: 7 }, () => backoff.recordFailure("sync:a").delayMs);
    expect(delays).toEqual([30_000, 60_000, 120_000, 240_000, 480_000, 600_000, 600_000]);
    expect(backoff.failures("sync:a")).toBe(7);
  });

  it("blocks attempts until the delay has passed", () => {
    const c = clock();
    const backoff = new MailboxBackoff({ baseMs: 30_000, maxMs: 600_000, jitter: 0, now: c.now });
    expect(backoff.isReady("sync:a")).toBe(true);
    backoff.recordFailure("sync:a");
    expect(backoff.isReady("sync:a")).toBe(false);
    c.advance(29_999);
    expect(backoff.isReady("sync:a")).toBe(false);
    c.advance(1);
    expect(backoff.isReady("sync:a")).toBe(true);
  });

  it("resets on success and reports the streak it ended", () => {
    const c = clock();
    const backoff = new MailboxBackoff({ baseMs: 1_000, maxMs: 10_000, jitter: 0, now: c.now });
    backoff.recordFailure("sync:a");
    backoff.recordFailure("sync:a");
    expect(backoff.recordSuccess("sync:a")).toBe(2);
    expect(backoff.isReady("sync:a")).toBe(true);
    expect(backoff.recordFailure("sync:a").delayMs).toBe(1_000);
    expect(backoff.recordSuccess("sync:never-failed")).toBe(0);
  });

  it("keeps mailboxes independent", () => {
    const c = clock();
    const backoff = new MailboxBackoff({ baseMs: 1_000, maxMs: 10_000, jitter: 0, now: c.now });
    backoff.recordFailure("sync:gmail");
    expect(backoff.isReady("sync:gmail")).toBe(false);
    expect(backoff.isReady("sync:seznam")).toBe(true);
    backoff.reset("sync:gmail");
    expect(backoff.isReady("sync:gmail")).toBe(true);
  });

  it("applies bounded jitter and never exceeds the cap", () => {
    const low = new MailboxBackoff({ baseMs: 10_000, maxMs: 15_000, jitter: 0.2, random: () => 0 });
    const high = new MailboxBackoff({ baseMs: 10_000, maxMs: 15_000, jitter: 0.2, random: () => 1 });
    expect(low.recordFailure("k").delayMs).toBe(8_000);
    expect(high.recordFailure("k").delayMs).toBe(12_000);
    high.recordFailure("k");
    expect(high.recordFailure("k").delayMs).toBe(15_000);
  });
});
