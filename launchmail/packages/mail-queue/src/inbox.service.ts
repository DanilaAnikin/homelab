import { db } from "@workspace/db";
import { incomingEmails, smtpConfigs } from "@workspace/db/schemas";
import { and, eq, desc, lt, or, ilike, sql, isNotNull } from "drizzle-orm";
import {
  INCOMING_ADDRESS_MAX_CHARS,
  INCOMING_ADDRESS_MAX_ITEMS,
  INCOMING_ATTACHMENT_MAX_ITEMS,
  INCOMING_CONTENT_TYPE_MAX_CHARS,
  INCOMING_FILENAME_MAX_CHARS,
  INCOMING_HEADER_MAX_CHARS,
  INCOMING_HTML_MAX_CHARS,
  INCOMING_NAME_MAX_CHARS,
  INCOMING_SUBJECT_MAX_CHARS,
  INCOMING_TEXT_MAX_CHARS,
} from "./incoming-email-limits";

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
    .select({
      id: incomingEmails.id,
      organizationId: incomingEmails.organizationId,
      smtpConfigId: incomingEmails.smtpConfigId,
      imapUid: incomingEmails.imapUid,
      messageId: sql<
        string | null
      >`left(${incomingEmails.messageId}, ${INCOMING_HEADER_MAX_CHARS})`,
      inReplyTo: sql<
        string | null
      >`left(${incomingEmails.inReplyTo}, ${INCOMING_HEADER_MAX_CHARS})`,
      references: sql<
        string | null
      >`left(${incomingEmails.references}, ${INCOMING_HEADER_MAX_CHARS})`,
      fromAddress: sql<string>`left(${incomingEmails.fromAddress}, ${INCOMING_ADDRESS_MAX_CHARS})`,
      fromName: sql<
        string | null
      >`left(${incomingEmails.fromName}, ${INCOMING_NAME_MAX_CHARS})`,
      toAddresses: sql<{ email: string; name?: string }[]>`coalesce((
        select jsonb_agg(value)
        from (
          select jsonb_strip_nulls(jsonb_build_object(
            'email', left(value ->> 'email', ${INCOMING_ADDRESS_MAX_CHARS}),
            'name', left(value ->> 'name', ${INCOMING_NAME_MAX_CHARS})
          )) as value
          from jsonb_array_elements(coalesce(${incomingEmails.toAddresses}, '[]'::jsonb)) as items(value)
          limit ${INCOMING_ADDRESS_MAX_ITEMS}
        ) as limited_to
      ), '[]'::jsonb)`,
      ccAddresses: sql<{ email: string; name?: string }[] | null>`case
        when ${incomingEmails.ccAddresses} is null then null
        else (
          select coalesce(jsonb_agg(value), '[]'::jsonb)
          from (
            select jsonb_strip_nulls(jsonb_build_object(
              'email', left(value ->> 'email', ${INCOMING_ADDRESS_MAX_CHARS}),
              'name', left(value ->> 'name', ${INCOMING_NAME_MAX_CHARS})
            )) as value
            from jsonb_array_elements(${incomingEmails.ccAddresses}) as items(value)
            limit ${INCOMING_ADDRESS_MAX_ITEMS}
          ) as limited_cc
        )
      end`,
      subject: sql<
        string | null
      >`left(${incomingEmails.subject}, ${INCOMING_SUBJECT_MAX_CHARS})`,
      snippet: sql<string | null>`left(${incomingEmails.snippet}, 240)`,
      text: sql<
        string | null
      >`left(${incomingEmails.text}, ${INCOMING_TEXT_MAX_CHARS})`,
      html: sql<
        string | null
      >`left(${incomingEmails.html}, ${INCOMING_HTML_MAX_CHARS})`,
      sourceSizeBytes: incomingEmails.sourceSizeBytes,
      sourceTruncated: incomingEmails.sourceTruncated,
      contentTruncated: sql<boolean>`(
        ${incomingEmails.contentTruncated}
        or coalesce(char_length(${incomingEmails.messageId}) > ${INCOMING_HEADER_MAX_CHARS}, false)
        or coalesce(char_length(${incomingEmails.inReplyTo}) > ${INCOMING_HEADER_MAX_CHARS}, false)
        or coalesce(char_length(${incomingEmails.references}) > ${INCOMING_HEADER_MAX_CHARS}, false)
        or char_length(${incomingEmails.fromAddress}) > ${INCOMING_ADDRESS_MAX_CHARS}
        or coalesce(char_length(${incomingEmails.fromName}) > ${INCOMING_NAME_MAX_CHARS}, false)
        or jsonb_array_length(coalesce(${incomingEmails.toAddresses}, '[]'::jsonb)) > ${INCOMING_ADDRESS_MAX_ITEMS}
        or jsonb_array_length(coalesce(${incomingEmails.ccAddresses}, '[]'::jsonb)) > ${INCOMING_ADDRESS_MAX_ITEMS}
        or exists (
          select 1 from (
            select value
            from jsonb_array_elements(coalesce(${incomingEmails.toAddresses}, '[]'::jsonb)) as items(value)
            limit ${INCOMING_ADDRESS_MAX_ITEMS}
          ) as bounded_to
          where char_length(value ->> 'email') > ${INCOMING_ADDRESS_MAX_CHARS}
             or char_length(value ->> 'name') > ${INCOMING_NAME_MAX_CHARS}
        )
        or exists (
          select 1 from (
            select value
            from jsonb_array_elements(coalesce(${incomingEmails.ccAddresses}, '[]'::jsonb)) as items(value)
            limit ${INCOMING_ADDRESS_MAX_ITEMS}
          ) as bounded_cc
          where char_length(value ->> 'email') > ${INCOMING_ADDRESS_MAX_CHARS}
             or char_length(value ->> 'name') > ${INCOMING_NAME_MAX_CHARS}
        )
        or coalesce(char_length(${incomingEmails.subject}) > ${INCOMING_SUBJECT_MAX_CHARS}, false)
        or coalesce(char_length(${incomingEmails.text}) > ${INCOMING_TEXT_MAX_CHARS}, false)
        or coalesce(char_length(${incomingEmails.html}) > ${INCOMING_HTML_MAX_CHARS}, false)
        or jsonb_array_length(coalesce(${incomingEmails.attachments}, '[]'::jsonb)) > ${INCOMING_ATTACHMENT_MAX_ITEMS}
        or exists (
          select 1 from (
            select value
            from jsonb_array_elements(coalesce(${incomingEmails.attachments}, '[]'::jsonb)) as items(value)
            limit ${INCOMING_ATTACHMENT_MAX_ITEMS}
          ) as bounded_attachments
          where char_length(value ->> 'filename') > ${INCOMING_FILENAME_MAX_CHARS}
             or char_length(value ->> 'contentType') > ${INCOMING_CONTENT_TYPE_MAX_CHARS}
        )
      )`,
      hasAttachments: incomingEmails.hasAttachments,
      attachments: sql<
        { filename: string; contentType: string; size: number }[] | null
      >`case
        when ${incomingEmails.attachments} is null then null
        else (
          select coalesce(jsonb_agg(value), '[]'::jsonb)
          from (
            select jsonb_build_object(
              'filename', left(coalesce(value ->> 'filename', 'attachment'), ${INCOMING_FILENAME_MAX_CHARS}),
              'contentType', left(coalesce(value ->> 'contentType', 'application/octet-stream'), ${INCOMING_CONTENT_TYPE_MAX_CHARS}),
              'size', coalesce(value -> 'size', '0'::jsonb)
            ) as value
            from jsonb_array_elements(${incomingEmails.attachments}) as items(value)
            limit ${INCOMING_ATTACHMENT_MAX_ITEMS}
          ) as limited_attachments
        )
      end`,
      seen: incomingEmails.seen,
      starred: incomingEmails.starred,
      archived: incomingEmails.archived,
      repliedAt: incomingEmails.repliedAt,
      receivedAt: incomingEmails.receivedAt,
      createdAt: incomingEmails.createdAt,
    })
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
