// IP/domain warm-up (Phase 4) — a brand-new sender that suddenly blasts volume
// looks exactly like a spammer and gets throttled or blocklisted. Warm-up ramps
// the daily send allowance as the sending domain ages, building reputation
// gradually. Direct delivery only; smarthost relays manage their own reputation.
import { getRedis } from "./redis";

const DAY_MS = 86_400_000;

// Max sends/day by age in days since first send. After the ladder → unlimited.
export const WARMUP_DAILY_CAPS = [
  50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000,
] as const;

export function warmupDailyCap(dayIndex: number): number {
  if (dayIndex < 0) return WARMUP_DAILY_CAPS[0];
  if (dayIndex >= WARMUP_DAILY_CAPS.length) return Infinity;
  return WARMUP_DAILY_CAPS[dayIndex]!;
}

/** Redis key for a sending domain's per-day counter. Exposed for tests. */
export function dayKey(domain: string, now: number): string {
  return `warmup:day:${domain.toLowerCase()}:${Math.floor(now / DAY_MS)}`;
}

export interface WarmupDecision {
  allowed: boolean;
  /** When not allowed, ms until the next day (when the quota resets). */
  resetInMs: number;
  cap: number;
  used: number;
}

/**
 * Try to consume one send from a sending domain's daily warm-up quota.
 * First send ever records the domain's start day (SETNX); the cap then follows
 * the ramp. Over the cap → deny until tomorrow.
 */
export async function consumeDailyQuota(
  sendingDomain: string,
  now: number = Date.now(),
): Promise<WarmupDecision> {
  const redis = getRedis();
  const d = sendingDomain.toLowerCase();
  const todayIndex = Math.floor(now / DAY_MS);

  const firstKey = `warmup:first:${d}`;
  await redis.setnx(firstKey, String(todayIndex));
  const firstDay = Number(await redis.get(firstKey));
  const cap = warmupDailyCap(
    Number.isFinite(firstDay) ? todayIndex - firstDay : 0,
  );
  if (!Number.isFinite(cap)) {
    return { allowed: true, resetInMs: 0, cap: Infinity, used: 0 };
  }

  const key = dayKey(d, now);
  const used = await redis.incr(key);
  if (used === 1) await redis.pexpire(key, DAY_MS);
  if (used <= cap) return { allowed: true, resetInMs: 0, cap, used };

  await redis.decr(key);
  const resetInMs = (todayIndex + 1) * DAY_MS - now;
  return { allowed: false, resetInMs: Math.max(1_000, resetInMs), cap, used: cap };
}
