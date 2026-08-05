import { createHash, randomUUID } from "node:crypto"

export interface EmailQueueOptionsInput {
  sendAt?: string | null
}

export interface EmailQueueJobOptions {
  jobId: string
  delay?: number
}

/**
 * BullMQ's default numeric ids restart when Redis queue state is recreated.
 * Assign every accepted mail a UUID so its durable PostgreSQL identity cannot
 * collide with an id from an earlier Redis epoch.
 */
export function createEmailJobId(): string {
  return randomUUID()
}

export function buildEmailQueueJobOptions(
  opts?: EmailQueueOptionsInput,
  jobId = createEmailJobId(),
  now = Date.now()
): EmailQueueJobOptions {
  let delay: number | undefined
  if (opts?.sendAt) {
    const ts = new Date(opts.sendAt).getTime()
    if (!Number.isNaN(ts)) delay = Math.max(0, ts - now)
  }
  return delay ? { jobId, delay } : { jobId }
}

/** Stable email-log UUID for every attempt of one globally unique queue job. */
export function emailLogIdForJob(jobId: string): string {
  const hash = createHash("sha256").update(jobId).digest("hex")
  return [
    hash.slice(0, 8),
    hash.slice(8, 12),
    `5${hash.slice(13, 16)}`,
    ((parseInt(hash.slice(16, 17), 16) & 0x3) | 0x8).toString(16) +
      hash.slice(17, 20),
    hash.slice(20, 32),
  ].join("-")
}
