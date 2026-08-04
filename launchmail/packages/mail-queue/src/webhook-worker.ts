import { createHmac } from "node:crypto"
import { Worker } from "bullmq"
import { db } from "@workspace/db"
import { webhooks } from "@workspace/db/schemas"
import type { Webhook, WebhookEvent } from "@workspace/db/schemas"
import { and, eq } from "drizzle-orm"
import { REDIS_URL } from "./redis"
import { ssrfSafePost, SsrfError } from "./webhooks.service"
import { WEBHOOK_QUEUE_NAME, type WebhookJobData } from "./webhook-queue"
import { completeWebhookOutbox, failWebhookOutbox } from "./webhook-outbox"

type DeliveryHook = Pick<
  Webhook,
  "id" | "organizationId" | "url" | "events" | "secret" | "enabled"
>

export interface WebhookDeliveryDependencies {
  loadHook: (
    hookId: string,
    organizationId: string
  ) => Promise<DeliveryHook | null>
  post: (
    url: string,
    body: string,
    headers: Record<string, string>,
    timeoutMs?: number
  ) => Promise<{ status: number }>
  recordAttempt: (
    hookId: string,
    organizationId: string,
    status: string,
    attemptedAt: Date
  ) => Promise<void>
  complete: (outboxId: string) => Promise<void>
  now: () => Date
}

const defaultDependencies: WebhookDeliveryDependencies = {
  async loadHook(hookId, organizationId) {
    const [hook] = await db
      .select()
      .from(webhooks)
      .where(
        and(
          eq(webhooks.id, hookId),
          eq(webhooks.organizationId, organizationId)
        )
      )
      .limit(1)
    return hook ?? null
  },
  post: ssrfSafePost,
  async recordAttempt(hookId, organizationId, status, attemptedAt) {
    await db
      .update(webhooks)
      .set({ lastStatus: status, lastDeliveredAt: attemptedAt })
      .where(
        and(
          eq(webhooks.id, hookId),
          eq(webhooks.organizationId, organizationId)
        )
      )
  },
  complete: completeWebhookOutbox,
  now: () => new Date(),
}

export type WebhookDeliveryResult =
  | { delivered: true; status: number }
  | { delivered: false; skipped: "deleted" | "disabled" | "event-disabled" }

function deliveryBody(
  event: WebhookEvent,
  data: unknown,
  occurredAt: string
): string {
  return JSON.stringify({
    event,
    createdAt: occurredAt,
    data,
  })
}

function failureStatus(error: unknown): string {
  if (error instanceof SsrfError) return "blocked"
  if (error instanceof Error && error.message === "timeout") return "timeout"
  return "error"
}

/**
 * Process one attempt. Throwing is deliberate: BullMQ retries every network
 * failure and every non-2xx response according to the queue's retry policy.
 */
export async function processWebhookDelivery(
  job: WebhookJobData,
  dependencies: WebhookDeliveryDependencies = defaultDependencies
): Promise<WebhookDeliveryResult> {
  const hook = await dependencies.loadHook(job.hookId, job.organizationId)
  if (!hook) {
    await dependencies.complete(job.outboxId)
    return { delivered: false, skipped: "deleted" }
  }
  if (!hook.enabled) {
    await dependencies.complete(job.outboxId)
    return { delivered: false, skipped: "disabled" }
  }
  if (!hook.events.includes(job.event)) {
    await dependencies.complete(job.outboxId)
    return { delivered: false, skipped: "event-disabled" }
  }

  const attemptedAt = dependencies.now()
  const body = deliveryBody(job.event, job.data, job.occurredAt)
  const signature = createHmac("sha256", hook.secret).update(body).digest("hex")

  let response: { status: number }
  try {
    response = await dependencies.post(
      hook.url,
      body,
      {
        "Content-Type": "application/json",
        "X-LaunchMail-Event": job.event,
        "X-LaunchMail-Signature": `sha256=${signature}`,
      },
      10_000
    )
  } catch (error) {
    await dependencies
      .recordAttempt(
        hook.id,
        hook.organizationId,
        failureStatus(error),
        attemptedAt
      )
      .catch(() => undefined)
    throw error
  }

  await dependencies
    .recordAttempt(
      hook.id,
      hook.organizationId,
      String(response.status),
      attemptedAt
    )
    .catch(() => undefined)

  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Webhook ${hook.id} responded with HTTP ${response.status}`)
  }

  await dependencies.complete(job.outboxId)
  return { delivered: true, status: response.status }
}

export function startWebhookWorker() {
  const worker = new Worker<WebhookJobData>(
    WEBHOOK_QUEUE_NAME,
    async (job) => processWebhookDelivery(job.data),
    {
      connection: { url: REDIS_URL },
      concurrency: 20,
    }
  )

  worker.on("error", (error) => {
    console.error(`[webhook-worker] Worker error: ${error.message}`)
  })
  worker.on("completed", (job, result) => {
    if (result.delivered) {
      console.log(
        `[webhook-worker] Delivered: ${job.id} (HTTP ${result.status})`
      )
    } else {
      console.log(`[webhook-worker] Skipped: ${job.id} (${result.skipped})`)
    }
  })
  worker.on("failed", (job, error) => {
    if (job) {
      console.error(
        `[webhook-worker] Attempt failed: ${job.id}: ${error.message}`
      )
      const maxAttempts = job.opts.attempts ?? 1
      if (job.attemptsMade >= maxAttempts) {
        void failWebhookOutbox(job.data.outboxId, error.message).catch(
          () => undefined
        )
      }
    }
  })

  console.log(
    `[webhook-worker] BullMQ worker started on queue '${WEBHOOK_QUEUE_NAME}'`
  )
  return worker
}
