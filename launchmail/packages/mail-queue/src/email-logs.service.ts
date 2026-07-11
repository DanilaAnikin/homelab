import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, desc, eq, inArray, lt, or, ilike, sql } from "drizzle-orm";

export async function listEmailLogs(organizationId: string, limit = 50) {
  return db
    .select()
    .from(emailLogs)
    .where(eq(emailLogs.organizationId, organizationId))
    .orderBy(desc(emailLogs.createdAt))
    .limit(limit);
}

export async function getEmailLogsByConfig(smtpConfigId: string, limit = 50) {
  return db
    .select()
    .from(emailLogs)
    .where(eq(emailLogs.smtpConfigId, smtpConfigId))
    .orderBy(desc(emailLogs.createdAt))
    .limit(limit);
}

export async function getEmailLogsByConfigs(
  smtpConfigIds: string[],
  limit = 50,
) {
  if (smtpConfigIds.length === 0) return [];
  return db
    .select()
    .from(emailLogs)
    .where(inArray(emailLogs.smtpConfigId, smtpConfigIds))
    .orderBy(desc(emailLogs.createdAt))
    .limit(limit);
}

export interface SentEmailSummary {
  id: string;
  smtpConfigId: string | null;
  to: { email: string; name?: string }[];
  subject: string;
  status: string;
  opens: number;
  clicks: number;
  createdAt: Date;
}

/**
 * Outgoing mail for the "Sent" view (everything we tried to send). Summary
 * columns only; the body is loaded by getSentEmail on open. Optional `q`
 * matches subject / from / recipient.
 */
export async function listSentEmails(
  organizationId: string,
  opts: { q?: string; limit?: number; before?: Date } = {},
): Promise<SentEmailSummary[]> {
  const limit = Math.min(100, Math.max(1, opts.limit ?? 50));
  const conds = [eq(emailLogs.organizationId, organizationId)];
  if (opts.before) conds.push(lt(emailLogs.createdAt, opts.before));
  if (opts.q && opts.q.trim()) {
    const like = `%${opts.q.trim()}%`;
    const term = or(
      ilike(emailLogs.subject, like),
      ilike(emailLogs.from, like),
      // `to` is a jsonb array of {email,name}; match its text form.
      ilike(sql`${emailLogs.to}::text`, like),
    );
    if (term) conds.push(term);
  }
  return db
    .select({
      id: emailLogs.id,
      smtpConfigId: emailLogs.smtpConfigId,
      to: emailLogs.to,
      subject: emailLogs.subject,
      status: emailLogs.status,
      opens: emailLogs.opens,
      clicks: emailLogs.clicks,
      createdAt: emailLogs.createdAt,
    })
    .from(emailLogs)
    .where(and(...conds))
    .orderBy(desc(emailLogs.createdAt))
    .limit(limit);
}

export async function getSentEmail(id: string, organizationId: string) {
  const rows = await db
    .select()
    .from(emailLogs)
    .where(
      and(eq(emailLogs.id, id), eq(emailLogs.organizationId, organizationId)),
    )
    .limit(1);
  return rows[0] ?? null;
}
