import { db } from "@workspace/db"
import { emailLogs } from "@workspace/db/schemas"
import type { WebhookEvent } from "@workspace/db/schemas"
import { eq, sql } from "drizzle-orm"
import type { EmailJobData } from "./queue"
import { assertExpectedEmailTerminalOutboxRows } from "./email-terminal-outbox-contract"
import {
  persistWebhookEvent,
  relayWebhookOutboxRows,
  type PersistedWebhookOutboxRow,
} from "./webhook-outbox"

export type EmailTerminalStatus = "sent" | "failed" | "bounced" | "suppressed"
export type EmailTerminalEvent = Extract<
  WebhookEvent,
  "email.sent" | "email.failed" | "email.bounced" | "email.suppressed"
>

export interface FinalizeEmailTerminalInput {
  trackingId: string
  jobId: string
  job: EmailJobData
  status: EmailTerminalStatus
  event: EmailTerminalEvent
  data: Record<string, unknown>
  error?: string | null
  providerMessageId?: string | null
  dkimSigned?: boolean
  occurredAt?: Date
  /**
   * Optional fail-closed contract for incident repair tooling. The assertion is
   * evaluated inside the same transaction as the email log and outbox writes,
   * so a hook race cannot leave a terminal log without its expected intent.
   */
  expectedOutboxRows?: number
}

export interface FinalizeEmailTerminalResult {
  deduplicated: boolean
  outboxRows: PersistedWebhookOutboxRow[]
}

const TERMINAL_STATUSES = new Set<EmailTerminalStatus>([
  "sent",
  "failed",
  "bounced",
  "suppressed",
])

/** Stable for every retry/finalizer invocation of one accepted mail job. */
export function emailTerminalIdempotencyKey(
  trackingId: string,
  event: EmailTerminalEvent
): string {
  return `email-terminal:${trackingId}:${event}`
}

export function shouldCommitEmailTerminal(
  existingStatus: string | null | undefined,
  nextStatus: EmailTerminalStatus
): boolean {
  if (!existingStatus) return true
  const existingTerminal = TERMINAL_STATUSES.has(
    existingStatus as EmailTerminalStatus
  )
  if (!existingTerminal) return true
  // A delivery can be accepted first and receive a permanent asynchronous DSN
  // later. Every other competing terminal transition is a duplicate/conflict.
  return existingStatus === "sent" && nextStatus === "bounced"
}

async function commitEmailTerminal(
  input: FinalizeEmailTerminalInput
): Promise<FinalizeEmailTerminalResult> {
  return db.transaction(async (tx) => {
    // Serialize competing finalizers for this deterministic log id. This keeps
    // synchronous terminal outcomes singular while still allowing sent →
    // bounced when a later asynchronous DSN arrives.
    await tx.execute(
      sql`select pg_advisory_xact_lock(hashtext(${input.trackingId}))`
    )
    const [existing] = await tx
      .select({ status: emailLogs.status })
      .from(emailLogs)
      .where(eq(emailLogs.id, input.trackingId))
      .limit(1)
    if (!shouldCommitEmailTerminal(existing?.status, input.status)) {
      return { deduplicated: true, outboxRows: [] }
    }

    const { job } = input
    await tx
      .insert(emailLogs)
      .values({
        id: input.trackingId,
        smtpConfigId: job.smtpConfigId,
        organizationId: job.organizationId ?? null,
        userId: job.userId ?? null,
        from: job.from,
        to: job.to,
        subject: job.subject,
        status: input.status,
        html: job.html ?? null,
        text: job.text ?? null,
        inReplyTo: job.inReplyTo ?? null,
        clientReference: job.clientReference ?? null,
        clientType: job.clientType ?? null,
        providerMessageId: input.providerMessageId ?? null,
        dkimSigned: input.dkimSigned ?? false,
        error: input.error ?? null,
      })
      .onConflictDoUpdate({
        target: emailLogs.id,
        set: {
          status: input.status,
          providerMessageId: input.providerMessageId ?? null,
          dkimSigned: input.dkimSigned ?? false,
          error: input.error ?? null,
        },
      })

    const outboxRows = job.organizationId
      ? await persistWebhookEvent(
          tx,
          job.organizationId,
          input.event,
          input.data,
          {
            occurredAt: input.occurredAt,
            idempotencyKey: emailTerminalIdempotencyKey(
              input.trackingId,
              input.event
            ),
          }
        )
      : []
    // Deliberately inside the transaction: throwing here rolls back both the
    // terminal email log and every outbox row created above.
    assertExpectedEmailTerminalOutboxRows(
      input.expectedOutboxRows,
      outboxRows.length
    )
    return { deduplicated: false, outboxRows }
  })
}

/**
 * Commit terminal mail state + webhook outbox atomically. Redis relay is only
 * an optimization; a scanner will recover any row that cannot be enqueued now.
 */
export async function finalizeEmailTerminal(
  input: FinalizeEmailTerminalInput
): Promise<FinalizeEmailTerminalResult> {
  const result = await commitEmailTerminal(input)
  if (result.outboxRows.length > 0) {
    await relayWebhookOutboxRows(result.outboxRows).catch((error) => {
      console.error(
        `[email-terminal] Outbox persisted but immediate relay failed: ${
          error instanceof Error ? error.message : String(error)
        }`
      )
    })
  }
  return result
}
