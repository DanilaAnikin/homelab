import { db } from "@workspace/db";
import { incomingEmails, smtpConfigs } from "@workspace/db/schemas";
import { and, eq, desc, lt, or, ilike, sql, isNotNull } from "drizzle-orm";

export type InboxFolder = "inbox" | "archived" | "starred" | "all";

export interface Mailbox {
  id: string;
  name: string;
  fromAddress: string;
  imapHost: string;
  unread: number;
  lastSyncAt: Date | null;
  lastSyncError: string | null;
}

export interface IncomingEmailSummary {
  id: string;
  smtpConfigId: string;
  fromAddress: string;
  fromName: string | null;
  subject: string | null;
  snippet: string | null;
  seen: boolean;
  starred: boolean;
  archived: boolean;
  hasAttachments: boolean;
  repliedAt: Date | null;
  receivedAt: Date;
}

/** Receive-enabled connections for an org, each with its unread count. */
export async function listMailboxes(organizationId: string): Promise<Mailbox[]> {
  const rows = await db
    .select({
      id: smtpConfigs.id,
      name: smtpConfigs.name,
      fromAddress: smtpConfigs.fromAddress,
      imapHost: smtpConfigs.imapHost,
      lastSyncAt: smtpConfigs.imapLastSyncAt,
      lastSyncError: smtpConfigs.imapLastSyncError,
      // Unread excludes archived, matching the default Inbox view.
      unread: sql<number>`count(${incomingEmails.id}) filter (where ${incomingEmails.seen} = false and ${incomingEmails.archived} = false)`,
    })
    .from(smtpConfigs)
    .leftJoin(
      incomingEmails,
      eq(incomingEmails.smtpConfigId, smtpConfigs.id),
    )
    .where(
      and(
        eq(smtpConfigs.organizationId, organizationId),
        isNotNull(smtpConfigs.imapHost),
      ),
    )
    .groupBy(smtpConfigs.id)
    .orderBy(desc(smtpConfigs.createdAt));

  return rows.map((r) => ({
    id: r.id,
    name: r.name,
    fromAddress: r.fromAddress,
    imapHost: r.imapHost ?? "",
    unread: Number(r.unread ?? 0),
    lastSyncAt: r.lastSyncAt,
    lastSyncError: r.lastSyncError,
  }));
}

/**
 * Inbox list (newest first). Summary columns only — bodies are loaded by
 * getIncomingEmail when a message is opened. `before` paginates via the
 * receivedAt of the last seen row.
 */
export async function listIncomingEmails(
  organizationId: string,
  opts: {
    smtpConfigId?: string;
    limit?: number;
    before?: Date;
    folder?: InboxFolder;
    q?: string;
  } = {},
): Promise<IncomingEmailSummary[]> {
  const limit = Math.min(100, Math.max(1, opts.limit ?? 50));
  const conds = [eq(incomingEmails.organizationId, organizationId)];
  if (opts.smtpConfigId)
    conds.push(eq(incomingEmails.smtpConfigId, opts.smtpConfigId));
  if (opts.before) conds.push(lt(incomingEmails.receivedAt, opts.before));

  // Folders: Inbox hides archived; Archived/Starred are their own views.
  switch (opts.folder) {
    case "archived":
      conds.push(eq(incomingEmails.archived, true));
      break;
    case "starred":
      conds.push(eq(incomingEmails.starred, true));
      break;
    case "all":
      break;
    default:
      conds.push(eq(incomingEmails.archived, false));
  }

  if (opts.q && opts.q.trim()) {
    const like = `%${opts.q.trim()}%`;
    const term = or(
      ilike(incomingEmails.fromAddress, like),
      ilike(incomingEmails.fromName, like),
      ilike(incomingEmails.subject, like),
      ilike(incomingEmails.snippet, like),
    );
    if (term) conds.push(term);
  }

  return db
    .select({
      id: incomingEmails.id,
      smtpConfigId: incomingEmails.smtpConfigId,
      fromAddress: incomingEmails.fromAddress,
      fromName: incomingEmails.fromName,
      subject: incomingEmails.subject,
      snippet: incomingEmails.snippet,
      seen: incomingEmails.seen,
      starred: incomingEmails.starred,
      archived: incomingEmails.archived,
      hasAttachments: incomingEmails.hasAttachments,
      repliedAt: incomingEmails.repliedAt,
      receivedAt: incomingEmails.receivedAt,
    })
    .from(incomingEmails)
    .where(and(...conds))
    .orderBy(desc(incomingEmails.receivedAt))
    .limit(limit);
}

export async function getIncomingEmail(id: string, organizationId: string) {
  const rows = await db
    .select()
    .from(incomingEmails)
    .where(
      and(
        eq(incomingEmails.id, id),
        eq(incomingEmails.organizationId, organizationId),
      ),
    )
    .limit(1);
  return rows[0] ?? null;
}

export async function setIncomingEmailSeen(
  id: string,
  organizationId: string,
  seen: boolean,
): Promise<boolean> {
  const rows = await db
    .update(incomingEmails)
    .set({ seen })
    .where(
      and(
        eq(incomingEmails.id, id),
        eq(incomingEmails.organizationId, organizationId),
      ),
    )
    .returning({ id: incomingEmails.id });
  return rows.length > 0;
}

export async function markIncomingEmailReplied(
  id: string,
  organizationId: string,
): Promise<void> {
  await db
    .update(incomingEmails)
    .set({ repliedAt: new Date() })
    .where(
      and(
        eq(incomingEmails.id, id),
        eq(incomingEmails.organizationId, organizationId),
      ),
    );
}

async function setFlag(
  id: string,
  organizationId: string,
  patch: Partial<{ starred: boolean; archived: boolean; seen: boolean }>,
): Promise<boolean> {
  const rows = await db
    .update(incomingEmails)
    .set(patch)
    .where(
      and(
        eq(incomingEmails.id, id),
        eq(incomingEmails.organizationId, organizationId),
      ),
    )
    .returning({ id: incomingEmails.id });
  return rows.length > 0;
}

export function setIncomingEmailStarred(
  id: string,
  organizationId: string,
  starred: boolean,
): Promise<boolean> {
  return setFlag(id, organizationId, { starred });
}

export function setIncomingEmailArchived(
  id: string,
  organizationId: string,
  archived: boolean,
): Promise<boolean> {
  return setFlag(id, organizationId, { archived });
}

/** Permanently remove the message from our store (does not touch the server
 *  copy; the UID cursor keeps it from being re-ingested). */
export async function deleteIncomingEmail(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(incomingEmails)
    .where(
      and(
        eq(incomingEmails.id, id),
        eq(incomingEmails.organizationId, organizationId),
      ),
    )
    .returning({ id: incomingEmails.id });
  return rows.length > 0;
}
