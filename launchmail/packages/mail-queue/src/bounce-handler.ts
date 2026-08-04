// Side effects for a parsed permanent bounce: mark the originating email log
// bounced, suppress the dead recipient, and fire an email.bounced webhook.
// Best-effort and idempotent-ish (re-suppression is a no-op).
import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, desc, eq, sql } from "drizzle-orm";
import { addSuppression } from "./suppressions.service";
import { dispatchEvent } from "./webhooks.service";
import {
  persistWebhookEvent,
  relayWebhookOutboxRows,
  type PersistedWebhookOutboxRow,
} from "./webhook-outbox";
import { emailTerminalIdempotencyKey } from "./email-terminal";
import type { BounceResult } from "./bounce";

export async function processBounce(
  organizationId: string,
  bounce: BounceResult,
): Promise<void> {
  if (!bounce.permanent) return;

  let recipient = bounce.recipient ?? null;
  let logId: string | null = null;
  let smtpConfigId: string | null = null;
  let clientReference: string | null = null;
  let clientType: string | null = null;

  // 1) VERP token → the exact email_logs id (most reliable attribution).
  if (bounce.verpToken) {
    const [row] = await db
      .select({
        id: emailLogs.id,
        to: emailLogs.to,
        organizationId: emailLogs.organizationId,
        smtpConfigId: emailLogs.smtpConfigId,
        clientReference: emailLogs.clientReference,
        clientType: emailLogs.clientType,
      })
      .from(emailLogs)
      .where(eq(emailLogs.id, bounce.verpToken))
      .limit(1);
    if (row && (!row.organizationId || row.organizationId === organizationId)) {
      logId = row.id;
      smtpConfigId = row.smtpConfigId;
      clientReference = row.clientReference;
      clientType = row.clientType;
      if (!recipient && row.to?.length) recipient = row.to[0]!.email;
    }
  }

  // 2) No VERP match → newest 'sent' log to this recipient in the org.
  if (!logId && recipient) {
    const [row] = await db
      .select({
        id: emailLogs.id,
        smtpConfigId: emailLogs.smtpConfigId,
        clientReference: emailLogs.clientReference,
        clientType: emailLogs.clientType,
      })
      .from(emailLogs)
      .where(
        and(
          eq(emailLogs.organizationId, organizationId),
          eq(emailLogs.status, "sent"),
          sql`${emailLogs.to} @> ${JSON.stringify([{ email: recipient }])}::jsonb`,
        ),
      )
      .orderBy(desc(emailLogs.createdAt))
      .limit(1);
    if (row) {
      logId = row.id;
      smtpConfigId = row.smtpConfigId;
      clientReference = row.clientReference;
      clientType = row.clientType;
    }
  }

  const eventData = {
    jobId: null,
    to: recipient ? [recipient] : [],
    status: bounce.status,
    diagnostic: bounce.diagnostic,
    // logId is deterministic from the original queue job and is the stable
    // correlation handle retained after the SMTP transaction completes.
    logId,
    smtpConfigId,
    // A DSN without the original RFC Message-ID must not substitute our
    // internal log UUID. Consumers use Message-ID for reply threading.
    messageId: null,
    clientReference,
    clientType,
  };

  if (logId) {
    let outboxRows: PersistedWebhookOutboxRow[] = [];
    await db.transaction(async (tx) => {
      await tx
        .update(emailLogs)
        .set({
          status: "bounced",
          error:
            bounce.diagnostic ??
            `bounced${bounce.status ? ` (${bounce.status})` : ""}`,
        })
        .where(eq(emailLogs.id, logId!));
      outboxRows = await persistWebhookEvent(
        tx,
        organizationId,
        "email.bounced",
        eventData,
        {
          idempotencyKey: emailTerminalIdempotencyKey(logId!, "email.bounced"),
        },
      );
    });
    await relayWebhookOutboxRows(outboxRows).catch(() => undefined);
  } else {
    // There is no related email_logs state to commit atomically, but the event
    // itself is still persisted to the outbox before any Redis operation.
    await dispatchEvent(organizationId, "email.bounced", eventData);
  }

  if (recipient) {
    await addSuppression(organizationId, recipient, "bounce").catch(
      () => undefined,
    );
  }
}
