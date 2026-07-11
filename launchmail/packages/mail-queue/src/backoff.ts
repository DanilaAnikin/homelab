// Greylisting-friendly retry schedule for mail delivery.
//
// A flat 5 s exponential is wrong for SMTP: greylisters deliberately defer the
// first attempt and expect you to come back MINUTES later, and transient MX
// issues (full mailbox, rate limit, brief outage) clear over hours, not
// seconds. This schedule spaces retries out so deferrals resolve naturally and
// we don't look like a hammering spammer.
//
// Index = attemptsMade - 1. After the last entry the job is out of attempts and
// fails permanently (queue `attempts` = length + 1).
export const MAIL_RETRY_SCHEDULE_MS = [
  60_000, //            +1 min
  5 * 60_000, //        +5 min
  15 * 60_000, //       +15 min
  60 * 60_000, //       +1 hour
  4 * 60 * 60_000, //   +4 hours
  8 * 60 * 60_000, //   +8 hours
  24 * 60 * 60_000, //  +24 hours (final)
] as const;

/** Total attempts a mail job gets (initial send + one per schedule entry). */
export const MAIL_JOB_ATTEMPTS = MAIL_RETRY_SCHEDULE_MS.length + 1;

/** Delay before the next retry, in ms, for a job that has made `attemptsMade` attempts. */
export function mailBackoffDelay(attemptsMade: number): number {
  const i = Math.min(
    Math.max(0, attemptsMade - 1),
    MAIL_RETRY_SCHEDULE_MS.length - 1,
  );
  return MAIL_RETRY_SCHEDULE_MS[i]!;
}
