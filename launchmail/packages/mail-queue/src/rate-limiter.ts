// Per-receiving-domain send-rate limiting for DIRECT delivery.
//
// Big providers throttle senders hard and punish bursts with tempfails or spam
// folder. Staying under a sane per-domain rate keeps our reputation clean.
// Smarthost sends skip this (the relay manages its own throughput).
//
// Implementation: a fixed 60-second window counter in Redis per domain. It's
// approximate (window edges), which is fine — the goal is smoothing bursts, not
// exact accounting. When a domain is over budget the worker re-delays the job
// (no retry consumed) until the window rolls over.
import { getRedis } from "./redis";

const WINDOW_MS = 60_000;
const DEFAULT_PER_MINUTE = 60;

// Conservative caps for the strict providers; everything else gets the default.
const PER_DOMAIN_PER_MINUTE: Record<string, number> = {
  "gmail.com": 20,
  "googlemail.com": 20,
  "outlook.com": 15,
  "hotmail.com": 15,
  "live.com": 15,
  "msn.com": 15,
  "yahoo.com": 15,
  "icloud.com": 15,
  "me.com": 15,
  "seznam.cz": 30,
  "email.cz": 30,
  "centrum.cz": 30,
};

/** Messages-per-minute cap for a receiving domain. */
export function limitForDomain(domain: string): number {
  return PER_DOMAIN_PER_MINUTE[domain.toLowerCase()] ?? DEFAULT_PER_MINUTE;
}

/** Redis key for the current fixed window of a domain. Exposed for tests. */
export function windowKey(domain: string, now: number): string {
  const windowStart = Math.floor(now / WINDOW_MS) * WINDOW_MS;
  return `rl:dom:${domain.toLowerCase()}:${windowStart}`;
}

export interface RateDecision {
  allowed: boolean;
  /** When not allowed, ms until the window resets and a retry makes sense. */
  retryAfterMs: number;
}

/**
 * Try to consume one send token for `domain` in the current window.
 * Over the cap → { allowed: false, retryAfterMs }. The over-count is rolled
 * back so a denied attempt doesn't permanently burn a slot.
 */
export async function consumeDomainToken(
  domain: string,
  now: number = Date.now(),
): Promise<RateDecision> {
  const limit = limitForDomain(domain);
  const windowStart = Math.floor(now / WINDOW_MS) * WINDOW_MS;
  const key = `rl:dom:${domain.toLowerCase()}:${windowStart}`;
  const redis = getRedis();

  const count = await redis.incr(key);
  if (count === 1) await redis.pexpire(key, WINDOW_MS);
  if (count <= limit) return { allowed: true, retryAfterMs: 0 };

  await redis.decr(key);
  const retryAfterMs = windowStart + WINDOW_MS - now;
  return { allowed: false, retryAfterMs: Math.max(1_000, retryAfterMs) };
}
