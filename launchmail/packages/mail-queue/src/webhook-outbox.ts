import { randomUUID } from "node:crypto"
import { db } from "@workspace/db"
import { webhookOutbox, webhooks } from "@workspace/db/schemas"
import type { WebhookEvent } from "@workspace/db/schemas"
import {
  and,
  asc,
  desc,
  eq,
  inArray,
  isNotNull,
  isNull,
  lt,
  or,
} from "drizzle-orm"
import { enqueueWebhook, webhookQueue } from "./webhook-queue"

export const WEBHOOK_OUTBOX_RELAY_STALE_MS = 5 * 60 * 1000

type DbTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0]
export type WebhookOutboxExecutor = typeof db | DbTransaction

export interface PersistWebhookEventOptions {
  occurredAt?: Date
  /** Stable source-event key. A per-hook suffix is added automatically. */
  idempotencyKey?: string
}

export interface PersistedWebhookOutboxRow {
  id: string
  hookId: string
  organizationId: string
  event: WebhookEvent
  data: unknown
  occurredAt: Date
}

export interface WebhookOutboxRelayState {
  queuedAt: Date | null
  completedAt: Date | null
  failedAt: Date | null
}

export function shouldRelayWebhookOutboxRow(
  row: WebhookOutboxRelayState,
  now = new Date()
): boolean {
  if (row.completedAt || row.failedAt) return false
  return (
    row.queuedAt === null ||
    row.queuedAt.getTime() < now.getTime() - WEBHOOK_OUTBOX_RELAY_STALE_MS
  )
}

export function buildWebhookOutboxJob(row: PersistedWebhookOutboxRow) {
  return {
    data: {
      outboxId: row.id,
      hookId: row.hookId,
      organizationId: row.organizationId,
      event: row.event,
      data: row.data,
      occurredAt: row.occurredAt.toISOString(),
    },
    options: { jobId: `outbox-${row.id}` },
  }
}

/**
 * Persist one row for every hook matching at event-commit time. Passing a DB
 * transaction makes the webhook intent atomic with the caller's state change.
 */
export async function persistWebhookEvent(
  executor: WebhookOutboxExecutor,
  organizationId: string,
  event: WebhookEvent,
  data: unknown,
  options: PersistWebhookEventOptions = {}
): Promise<PersistedWebhookOutboxRow[]> {
  const hooks = await executor
    .select({ id: webhooks.id, events: webhooks.events })
    .from(webhooks)
    .where(
      and(
        eq(webhooks.organizationId, organizationId),
        eq(webhooks.enabled, true)
      )
    )
  const matching = hooks.filter((hook) => hook.events.includes(event))
  if (matching.length === 0) return []

  const occurredAt = options.occurredAt ?? new Date()
  const sourceKey = options.idempotencyKey ?? randomUUID()
  return executor
    .insert(webhookOutbox)
    .values(
      matching.map((hook) => ({
        organizationId,
        hookId: hook.id,
        event,
        data,
        occurredAt,
        idempotencyKey: `${sourceKey}:${hook.id}`,
      }))
    )
    .onConflictDoNothing({ target: webhookOutbox.idempotencyKey })
    .returning({
      id: webhookOutbox.id,
      hookId: webhookOutbox.hookId,
      organizationId: webhookOutbox.organizationId,
      event: webhookOutbox.event,
      data: webhookOutbox.data,
      occurredAt: webhookOutbox.occurredAt,
    }) as Promise<PersistedWebhookOutboxRow[]>
}

async function markRowsMissingHooks(ids: string[]): Promise<void> {
  if (ids.length === 0) return
  await db
    .update(webhookOutbox)
    .set({ completedAt: new Date(), lastError: "webhook deleted before relay" })
    .where(inArray(webhookOutbox.id, ids))
}

export async function relayWebhookOutboxRows(
  rows: PersistedWebhookOutboxRow[]
): Promise<void> {
  for (const row of rows) {
    if (!row.hookId) {
      await markRowsMissingHooks([row.id])
      continue
    }
    try {
      const job = buildWebhookOutboxJob(row)
      // Deterministic: a crash after Queue.add but before queued_at is safe.
      await enqueueWebhook(job.data, job.options)
      await db
        .update(webhookOutbox)
        .set({ queuedAt: new Date(), lastError: null })
        .where(eq(webhookOutbox.id, row.id))
    } catch (error) {
      await db
        .update(webhookOutbox)
        .set({
          lastError: error instanceof Error ? error.message : String(error),
        })
        .where(eq(webhookOutbox.id, row.id))
        .catch(() => undefined)
      throw error
    }
  }
}

export async function relayPendingWebhookOutbox(
  limit = 100,
  now = new Date()
): Promise<number> {
  // queued_at is a renewable relay lease, not proof that Redis still owns the
  // job. Re-adding the deterministic outbox id after the lease expires is safe:
  // BullMQ keeps an existing live job singular and recreates a missing one.
  const staleBefore = new Date(now.getTime() - WEBHOOK_OUTBOX_RELAY_STALE_MS)
  const rows = await db
    .select({
      id: webhookOutbox.id,
      hookId: webhookOutbox.hookId,
      organizationId: webhookOutbox.organizationId,
      event: webhookOutbox.event,
      data: webhookOutbox.data,
      occurredAt: webhookOutbox.occurredAt,
    })
    .from(webhookOutbox)
    .where(
      and(
        or(
          isNull(webhookOutbox.queuedAt),
          lt(webhookOutbox.queuedAt, staleBefore)
        ),
        isNull(webhookOutbox.completedAt),
        isNull(webhookOutbox.failedAt)
      )
    )
    .orderBy(asc(webhookOutbox.createdAt))
    .limit(limit)

  const missingHookIds = rows.filter((row) => !row.hookId).map((row) => row.id)
  await markRowsMissingHooks(missingHookIds)
  const deliverable = rows.filter(
    (row): row is PersistedWebhookOutboxRow => row.hookId !== null
  )
  await relayWebhookOutboxRows(deliverable)
  return rows.length
}

export async function completeWebhookOutbox(id: string): Promise<void> {
  await db
    .update(webhookOutbox)
    .set({ completedAt: new Date(), failedAt: null, lastError: null })
    .where(eq(webhookOutbox.id, id))
}

export async function failWebhookOutbox(
  id: string,
  error: string
): Promise<void> {
  await db
    .update(webhookOutbox)
    .set({ failedAt: new Date(), lastError: error })
    .where(eq(webhookOutbox.id, id))
}

export async function listFailedWebhookOutbox(
  organizationId: string,
  limit = 100
) {
  return db
    .select({
      id: webhookOutbox.id,
      hookId: webhookOutbox.hookId,
      event: webhookOutbox.event,
      occurredAt: webhookOutbox.occurredAt,
      failedAt: webhookOutbox.failedAt,
      lastError: webhookOutbox.lastError,
    })
    .from(webhookOutbox)
    .where(
      and(
        eq(webhookOutbox.organizationId, organizationId),
        isNotNull(webhookOutbox.failedAt)
      )
    )
    .orderBy(desc(webhookOutbox.failedAt))
    .limit(Math.min(100, Math.max(1, limit)))
}

export async function replayFailedWebhookOutbox(
  id: string,
  organizationId: string
): Promise<boolean> {
  const [row] = await db
    .select({
      id: webhookOutbox.id,
      hookId: webhookOutbox.hookId,
      organizationId: webhookOutbox.organizationId,
      event: webhookOutbox.event,
      data: webhookOutbox.data,
      occurredAt: webhookOutbox.occurredAt,
    })
    .from(webhookOutbox)
    .where(
      and(
        eq(webhookOutbox.id, id),
        eq(webhookOutbox.organizationId, organizationId),
        isNotNull(webhookOutbox.failedAt)
      )
    )
    .limit(1)
  if (!row) return false
  if (!row.hookId) {
    await markRowsMissingHooks([row.id])
    return true
  }

  const existingJob = await webhookQueue.getJob(`outbox-${row.id}`)
  if (existingJob) {
    const state = await existingJob.getState()
    if (state === "failed") {
      await existingJob.retry()
      await db
        .update(webhookOutbox)
        .set({
          queuedAt: new Date(),
          completedAt: null,
          failedAt: null,
          lastError: null,
        })
        .where(eq(webhookOutbox.id, id))
      return true
    }
    if (state === "completed") {
      await completeWebhookOutbox(id)
      return true
    }
    // waiting/delayed/active already has a live delivery attempt.
    await db
      .update(webhookOutbox)
      .set({
        queuedAt: new Date(),
        completedAt: null,
        failedAt: null,
        lastError: null,
      })
      .where(eq(webhookOutbox.id, id))
    return true
  }

  await db
    .update(webhookOutbox)
    .set({
      queuedAt: null,
      completedAt: null,
      failedAt: null,
      lastError: null,
    })
    .where(eq(webhookOutbox.id, id))
  await relayWebhookOutboxRows([{ ...row, hookId: row.hookId }])
  return true
}

export interface WebhookOutboxRelay {
  stop(): Promise<void>
}

export function startWebhookOutboxRelay(
  intervalMs = 5_000
): WebhookOutboxRelay {
  let stopped = false
  let running: Promise<void> | null = null

  const tick = () => {
    if (stopped || running) return
    running = relayPendingWebhookOutbox()
      .then((count) => {
        if (count > 0) console.log(`[webhook-outbox] Relayed ${count} row(s)`)
      })
      .catch((error) => {
        console.error(
          `[webhook-outbox] Relay failed: ${error instanceof Error ? error.message : String(error)}`
        )
      })
      .finally(() => {
        running = null
      })
  }

  tick()
  const timer = setInterval(tick, intervalMs)
  timer.unref()

  return {
    async stop() {
      stopped = true
      clearInterval(timer)
      await running
    },
  }
}
