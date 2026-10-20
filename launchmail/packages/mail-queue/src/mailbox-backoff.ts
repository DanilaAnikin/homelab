// Per-mailbox exponential backoff for the IMAP worker.
//
// Without it a failing mailbox is retried on every trigger — each IDLE
// `exists` push, every 60 s reconnect and every safety poll — which turns a
// throttled or misconfigured account into a reconnect storm (and, for Gmail,
// more throttling). One mailbox's failures never delay another mailbox: the
// state is keyed per mailbox and operation.

export interface BackoffOptions {
  /** Delay after the first failure. */
  baseMs: number;
  /** Upper bound for the delay. */
  maxMs: number;
  /** ± fraction applied to each delay so mailboxes on one server spread out. */
  jitter?: number;
  now?: () => number;
  random?: () => number;
}

interface Entry {
  failures: number;
  nextAttemptAt: number;
}

export class MailboxBackoff {
  private readonly entries = new Map<string, Entry>();
  private readonly baseMs: number;
  private readonly maxMs: number;
  private readonly jitter: number;
  private readonly now: () => number;
  private readonly random: () => number;

  constructor(options: BackoffOptions) {
    this.baseMs = Math.max(1, options.baseMs);
    this.maxMs = Math.max(this.baseMs, options.maxMs);
    this.jitter = Math.min(0.5, Math.max(0, options.jitter ?? 0.2));
    this.now = options.now ?? Date.now;
    this.random = options.random ?? Math.random;
  }

  /** May this key be attempted now? */
  isReady(key: string): boolean {
    const entry = this.entries.get(key);
    return !entry || this.now() >= entry.nextAttemptAt;
  }

  /** Consecutive failures recorded for this key (0 after a success). */
  failures(key: string): number {
    return this.entries.get(key)?.failures ?? 0;
  }

  /** Record a failure; returns the new streak and the delay until the next attempt. */
  recordFailure(key: string): { failures: number; delayMs: number } {
    const failures = this.failures(key) + 1;
    // 2^(n-1) grows past maxMs long before it could overflow; cap the exponent anyway.
    const raw = Math.min(this.maxMs, this.baseMs * 2 ** Math.min(failures - 1, 30));
    const factor = 1 + (this.random() * 2 - 1) * this.jitter;
    const delayMs = Math.round(Math.min(this.maxMs, raw * factor));
    this.entries.set(key, { failures, nextAttemptAt: this.now() + delayMs });
    return { failures, delayMs };
  }

  /** Record a success; returns how many failures in a row preceded it. */
  recordSuccess(key: string): number {
    const previous = this.failures(key);
    this.entries.delete(key);
    return previous;
  }

  /** Forget a key (mailbox removed or its credentials changed). */
  reset(key: string): void {
    this.entries.delete(key);
  }
}
