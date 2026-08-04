import { Queue } from "bullmq"
import type { WebhookEvent } from "@workspace/db/schemas"
import { REDIS_URL } from "./redis"

export const WEBHOOK_QUEUE_NAME = "webhook-delivery"
export const WEBHOOK_JOB_ATTEMPTS = 8

/**
 * Durable webhook job data. The signing secret intentionally never leaves the
 * database: the worker reloads the hook immediately before every attempt.
 */
export interface WebhookJobData {
  outboxId: string
  hookId: string
  organizationId: string
  event: WebhookEvent
  data: unknown
  /** Immutable event time captured before the first delivery attempt. */
  occurredAt: string
}

export const webhookQueue = new Queue<WebhookJobData>(WEBHOOK_QUEUE_NAME, {
  connection: { url: REDIS_URL },
  defaultJobOptions: {
    attempts: WEBHOOK_JOB_ATTEMPTS,
    backoff: {
      type: "exponential",
      delay: 30_000,
    },
    // Keep completed idempotency keys longer than normal outage/redeploy
    // windows so an outbox replay cannot create a duplicate BullMQ job.
    removeOnComplete: { age: 3600 * 24 * 30 },
    removeOnFail: { age: 3600 * 24 * 7 },
  },
})

export async function enqueueWebhook(
  data: WebhookJobData,
  options?: { jobId?: string }
) {
  return webhookQueue.add("deliver-webhook", data, options)
}

/** Close the producer connection during process shutdown. */
export async function closeWebhookQueue(): Promise<void> {
  await webhookQueue.close()
}
