// Side effects for a parsed permanent bounce: mark the originating email log
// bounced, suppress the dead recipient, and fire an email.bounced webhook.
// Best-effort and idempotent-ish (re-suppression is a no-op).
import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, desc, eq, sql } from "drizzle-orm";
import { addSuppression } from "./suppressions.service";
import { dispatchEvent } from "./webhooks.service";
import type { BounceResult } from "./bounce";

export async function processBounce(
  organizationId: string,
  bounce: BounceResult,
): Promise<void> {
  if (!bounce.permanent) return;

  let recipient = bounce.recipient ?? null;
  let logId: string | null = null;

  // 1) VERP token → the exact email_logs id (most reliable attribution).
  if (bounce.verpToken) {
    const [row] = await db
      .select({
        id: emailLogs.id,
        to: emailLogs.to,
        organizationId: emailLogs.organizationId,
      })
      .from(emailLogs)
      .where(eq(emailLogs.id, bounce.verpToken))
      .limit(1);
    if (row && (!row.organizationId || row.organizationId === organizationId)) {
      logId = row.id;
      if (!recipient && row.to?.length) recipient = row.to[0]!.email;
    }
  }

  // 2) No VERP match → newest 'sent' log to this recipient in the org.
  if (!logId && recipient) {
    const [row] = await db
      .select({ id: emailLogs.id })
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
    if (row) logId = row.id;
  }

  if (logId) {
    await db
      .update(emailLogs)
      .set({
        status: "bounced",
        error:
          bounce.diagnostic ??
          `bounced${bounce.status ? ` (${bounce.status})` : ""}`,
      })
      .where(eq(emailLogs.id, logId));
  }

  if (recipient) {
    await addSuppression(organizationId, recipient, "bounce").catch(
      () => undefined,
    );
  }

  void dispatchEvent(organizationId, "email.bounced", {
    to: recipient ? [recipient] : [],
    status: bounce.status,
    diagnostic: bounce.diagnostic,
    messageId: logId,
  }).catch(() => undefined);
}
