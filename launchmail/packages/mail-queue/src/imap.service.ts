import {
  ImapFlow,
  type ImapFlowOptions,
  type MessageAddressObject,
  type MessageEnvelopeObject,
} from "imapflow";
import { simpleParser, type AddressObject, type ParsedMail } from "mailparser";
import { db } from "@workspace/db";
import { incomingEmails } from "@workspace/db/schemas";
import type { SmtpConfig } from "./smtp-configs.service";
import { updateImapState } from "./smtp-configs.service";
import { dispatchEvent } from "./webhooks.service";
import { parseBounce, type BounceResult } from "./bounce";
import { processBounce } from "./bounce-handler";
import {
  INCOMING_ADDRESS_MAX_CHARS,
  INCOMING_ADDRESS_MAX_ITEMS,
  INCOMING_ATTACHMENT_MAX_BYTES,
  INCOMING_ATTACHMENT_MAX_ITEMS,
  INCOMING_AUTOMATION_HEADER_MAX_CHARS,
  INCOMING_CONTENT_TYPE_MAX_CHARS,
  INCOMING_FILENAME_MAX_CHARS,
  INCOMING_HEADER_MAX_CHARS,
  INCOMING_HTML_MAX_CHARS,
  INCOMING_NAME_MAX_CHARS,
  INCOMING_PROTOCOL_MAX_LINE_BYTES,
  INCOMING_SOURCE_MAX_BYTES,
  INCOMING_SUBJECT_MAX_CHARS,
  INCOMING_TEXT_MAX_CHARS,
} from "./incoming-email-limits";

// On the very first sync of a mailbox we pull the most recent N messages so the
// inbox is useful immediately without dragging in years of history in one shot.
// Subsequent polls only fetch UIDs above the stored cursor, so the rest of the
// archive is left alone unless explicitly backfilled later.
const INITIAL_SYNC_LIMIT = 200;
// Hard cap on messages ingested in a single poll. Each row is individually
// bounded, and this batch ceiling also keeps the accumulated parsed rows below
// a predictable memory budget. The remainder lands on the next tick.
const MAX_PER_SYNC = 50;

type Addr = { email: string; name?: string };

interface Bounded<T> {
  value: T;
  truncated: boolean;
}

function boundString(value: string, max: number): Bounded<string> {
  return value.length > max
    ? { value: value.slice(0, max), truncated: true }
    : { value, truncated: false };
}

type AutomationHeaderName =
  | "auto-submitted"
  | "precedence"
  | "x-auto-response-suppress";

/**
 * Read one explicitly allowlisted automation-safety header from mailparser's
 * case-folded header map. Values are unfolded, whitespace-normalized and
 * lower-cased so downstream guards cannot accidentally compare them
 * case-sensitively. Unexpected structured values fail closed by marking the
 * message content truncated instead of serializing arbitrary header objects.
 */
function automationHeader(
  parsed: ParsedMail | null,
  name: AutomationHeaderName,
): Bounded<string | null> {
  if (!parsed) return { value: null, truncated: false };
  const raw = parsed.headers.get(name);
  if (raw == null) return { value: null, truncated: false };

  let value: string;
  if (typeof raw === "string") {
    value = raw;
  } else if (
    Array.isArray(raw) &&
    raw.every((item): item is string => typeof item === "string")
  ) {
    value = raw.join(", ");
  } else {
    return { value: null, truncated: true };
  }

  const normalized = value
    .replace(/[\t\r\n ]+/g, " ")
    .trim()
    .toLowerCase();
  if (!normalized) return { value: null, truncated: false };
  const bounded = boundString(normalized, INCOMING_AUTOMATION_HEADER_MAX_CHARS);
  return { value: bounded.value, truncated: bounded.truncated };
}

function toAddrs(obj?: AddressObject | AddressObject[]): Bounded<Addr[]> {
  if (!obj) return { value: [], truncated: false };
  const list = Array.isArray(obj) ? obj : [obj];
  const out: Addr[] = [];
  let truncated = false;
  for (const o of list) {
    for (const a of o.value ?? []) {
      if (!a.address) continue;
      if (out.length >= INCOMING_ADDRESS_MAX_ITEMS) {
        truncated = true;
        break;
      }
      const email = boundString(a.address, INCOMING_ADDRESS_MAX_CHARS);
      const name = a.name ? boundString(a.name, INCOMING_NAME_MAX_CHARS) : null;
      truncated ||= email.truncated || Boolean(name?.truncated);
      out.push(
        name
          ? { email: email.value, name: name.value }
          : { email: email.value },
      );
    }
  }
  return { value: out, truncated };
}

function envelopeAddrs(items?: MessageAddressObject[]): Bounded<Addr[]> {
  if (!items) return { value: [], truncated: false };
  const out: Addr[] = [];
  let truncated = false;
  for (const item of items) {
    if (!item.address) continue;
    if (out.length >= INCOMING_ADDRESS_MAX_ITEMS) {
      truncated = true;
      break;
    }
    const email = boundString(item.address, INCOMING_ADDRESS_MAX_CHARS);
    const name = item.name
      ? boundString(item.name, INCOMING_NAME_MAX_CHARS)
      : null;
    truncated ||= email.truncated || Boolean(name?.truncated);
    out.push(
      name ? { email: email.value, name: name.value } : { email: email.value },
    );
  }
  return { value: out, truncated };
}

function buildSnippet(text: string | null, subject: string | null): string {
  // Normalize only a small prefix; running the regex across the entire body
  // would create another large temporary string before we slice it.
  const base = (text ?? subject ?? "")
    .slice(0, 4 * 1024)
    .replace(/\s+/g, " ")
    .trim();
  return base.slice(0, 240);
}

function safeSourceSize(value: number | undefined, fallback: number): number {
  if (!Number.isFinite(value) || value == null || value < 0) return fallback;
  return Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(value));
}

function safeDate(...values: Array<Date | string | undefined>): Date {
  for (const value of values) {
    if (value == null) continue;
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date;
  }
  return new Date();
}

// Escape hatch for self-hosted mail servers with self-signed certificates.
// Defaults to verifying certs (secure); set INBOX_IMAP_REJECT_UNAUTHORIZED=false
// only when you trust the server and it presents an untrusted cert.
const TLS_REJECT_UNAUTHORIZED =
  process.env.INBOX_IMAP_REJECT_UNAUTHORIZED !== "false";

/** Build an ImapFlow client from a receive-enabled SmtpConfig. */
function clientFor(config: SmtpConfig): ImapFlow {
  const opts: ImapFlowOptions = {
    host: config.imapHost!,
    port: config.imapPort ?? 993,
    secure: config.imapSecure ?? true,
    auth: {
      user: config.imapUsername || config.username || "",
      pass: config.imapPassword ?? "",
    },
    logger: false,
    tls: { rejectUnauthorized: TLS_REJECT_UNAUTHORIZED },
    // Keep a stuck server from hanging the poller; fail fast and retry next tick.
    greetingTimeout: 15000,
    socketTimeout: 60000,
    connectionTimeout: 15000,
    maxLiteralSize: INCOMING_SOURCE_MAX_BYTES,
    maxLineLength: INCOMING_PROTOCOL_MAX_LINE_BYTES,
  };
  return new ImapFlow(opts);
}

export interface SyncResult {
  fetched: number;
  error?: string;
}

type InsertRow = typeof incomingEmails.$inferInsert;

/** Parse one fetched IMAP message into an incoming_emails insert row. */
async function parseMessageToRow(
  config: SmtpConfig,
  uid: number,
  source: Buffer,
  internalDate?: string | Date,
  bounces?: Map<number, BounceResult>,
  announcedSize?: number,
  envelope?: MessageEnvelopeObject,
): Promise<InsertRow | null> {
  const sourceSizeBytes = safeSourceSize(announcedSize, source.length);
  const sourceTruncated =
    sourceSizeBytes > source.length ||
    (announcedSize == null && source.length >= INCOMING_SOURCE_MAX_BYTES);

  let parsed: ParsedMail | null = null;
  try {
    parsed = await simpleParser(source, {
      // Avoid secondary body inflation. The bounded HTML remains available to
      // callers; we do not synthesize another text copy or inline CID images.
      skipHtmlToText: true,
      skipImageLinks: true,
    });
  } catch {
    // A partial MIME tail can be syntactically incomplete. Preserve a safe
    // envelope-only row for an oversized message instead of silently losing it.
    if (!sourceTruncated) return null;
  }
  // Bounce detection: collect DSNs so the caller can suppress dead recipients
  // after ingest. Never act on an incomplete DSN because its terminal fields
  // may have been cut off by the source ceiling.
  if (bounces && parsed && !sourceTruncated) {
    const b = parseBounce(parsed);
    if (b.isBounce) bounces.set(uid, b);
  }

  const parsedFrom = parsed ? toAddrs(parsed.from) : null;
  const fallbackFrom = envelopeAddrs(envelope?.from);
  const from = parsedFrom?.value[0] ?? fallbackFrom.value[0];
  const to = parsed ? toAddrs(parsed.to) : envelopeAddrs(envelope?.to);
  const cc = parsed ? toAddrs(parsed.cc) : envelopeAddrs(envelope?.cc);

  const subject = boundString(
    parsed?.subject ?? envelope?.subject ?? "",
    INCOMING_SUBJECT_MAX_CHARS,
  );
  const messageId = boundString(
    parsed?.messageId ?? envelope?.messageId ?? "",
    INCOMING_HEADER_MAX_CHARS,
  );
  const inReplyTo = boundString(
    parsed?.inReplyTo ?? envelope?.inReplyTo ?? "",
    INCOMING_HEADER_MAX_CHARS,
  );
  const rawReferences = Array.isArray(parsed?.references)
    ? parsed.references.join(" ")
    : (parsed?.references ?? "");
  const references = boundString(rawReferences, INCOMING_HEADER_MAX_CHARS);
  const autoSubmitted = automationHeader(parsed, "auto-submitted");
  const precedence = automationHeader(parsed, "precedence");
  const xAutoResponseSuppress = automationHeader(
    parsed,
    "x-auto-response-suppress",
  );

  const rawText = parsed?.text ?? null;
  const text = rawText
    ? boundString(rawText, INCOMING_TEXT_MAX_CHARS)
    : { value: null, truncated: false };
  const rawHtml = parsed?.html === false ? null : (parsed?.html ?? null);
  const html = rawHtml
    ? boundString(rawHtml, INCOMING_HTML_MAX_CHARS)
    : { value: null, truncated: false };

  const attachments: NonNullable<InsertRow["attachments"]> = [];
  let attachmentsTruncated = false;
  for (const attachment of parsed?.attachments ?? []) {
    if (!attachment.filename && attachment.related !== false) continue;
    if (attachments.length >= INCOMING_ATTACHMENT_MAX_ITEMS) {
      attachmentsTruncated = true;
      break;
    }
    const filename = boundString(
      attachment.filename ?? "attachment",
      INCOMING_FILENAME_MAX_CHARS,
    );
    const contentType = boundString(
      attachment.contentType ?? "application/octet-stream",
      INCOMING_CONTENT_TYPE_MAX_CHARS,
    );
    attachmentsTruncated ||=
      filename.truncated ||
      contentType.truncated ||
      (attachment.size ?? 0) > INCOMING_ATTACHMENT_MAX_BYTES;
    attachments.push({
      filename: filename.value,
      contentType: contentType.value,
      size: Math.max(
        0,
        Math.min(Number.MAX_SAFE_INTEGER, attachment.size ?? 0),
      ),
    });
  }

  const fromEmail = boundString(
    from?.email ?? "unknown@unknown",
    INCOMING_ADDRESS_MAX_CHARS,
  );
  const fromName = from?.name
    ? boundString(from.name, INCOMING_NAME_MAX_CHARS)
    : null;
  const contentTruncated =
    sourceTruncated ||
    Boolean(parsedFrom?.truncated) ||
    fallbackFrom.truncated ||
    to.truncated ||
    cc.truncated ||
    subject.truncated ||
    messageId.truncated ||
    inReplyTo.truncated ||
    references.truncated ||
    autoSubmitted.truncated ||
    precedence.truncated ||
    xAutoResponseSuppress.truncated ||
    text.truncated ||
    html.truncated ||
    attachmentsTruncated ||
    fromEmail.truncated ||
    Boolean(fromName?.truncated);

  return {
    organizationId: config.organizationId,
    smtpConfigId: config.id,
    imapUid: uid,
    messageId: messageId.value || null,
    inReplyTo: inReplyTo.value || null,
    references: references.value || null,
    autoSubmitted: autoSubmitted.value,
    precedence: precedence.value,
    xAutoResponseSuppress: xAutoResponseSuppress.value,
    // This bit distinguishes a parsed message with no automation headers from
    // a legacy row whose source was never projected through this parser.
    automationHeadersComplete: parsed !== null,
    fromAddress: fromEmail.value,
    fromName: fromName?.value ?? null,
    toAddresses: to.value,
    ccAddresses: cc.value,
    subject: subject.value || null,
    snippet: buildSnippet(text.value, subject.value || null),
    text: text.value,
    html: html.value,
    sourceSizeBytes,
    sourceTruncated,
    contentTruncated,
    hasAttachments: attachments.length > 0,
    attachments: attachments.length > 0 ? attachments : null,
    receivedAt: safeDate(parsed?.date, envelope?.date, internalDate),
  };
}

interface InsertedRow {
  id: string;
  imapUid: number;
  fromAddress: string;
  fromName: string | null;
  subject: string | null;
  receivedAt: Date;
  sourceTruncated: boolean;
  contentTruncated: boolean;
}

/** Insert rows in small chunks; returns ONLY the rows genuinely inserted (the
 *  unique index drops re-ingested messages), so callers can fire side effects
 *  (webhooks) exactly once per new message. */
async function ingest(rows: InsertRow[]): Promise<InsertedRow[]> {
  const inserted: InsertedRow[] = [];
  for (let i = 0; i < rows.length; i += 50) {
    const chunk = await db
      .insert(incomingEmails)
      .values(rows.slice(i, i + 50))
      .onConflictDoNothing({
        target: [incomingEmails.smtpConfigId, incomingEmails.imapUid],
      })
      .returning({
        id: incomingEmails.id,
        imapUid: incomingEmails.imapUid,
        fromAddress: incomingEmails.fromAddress,
        fromName: incomingEmails.fromName,
        subject: incomingEmails.subject,
        receivedAt: incomingEmails.receivedAt,
        sourceTruncated: incomingEmails.sourceTruncated,
        contentTruncated: incomingEmails.contentTruncated,
      });
    inserted.push(...chunk);
  }
  return inserted;
}

/**
 * Pull NEW mail (UIDs above the cursor) for one receive-enabled connection into
 * incoming_emails. Idempotent: (smtp_config_id, imap_uid) is unique. Advances
 * the forward cursor, lowers the backfill cursor (firstUid) on first sync,
 * records sync health, and fires `incoming.received` per genuinely-new message.
 */
export async function syncMailbox(config: SmtpConfig): Promise<SyncResult> {
  if (!config.imapHost) return { fetched: 0 };
  if (!config.imapPassword) {
    return { fetched: 0, error: "IMAP password not set" };
  }

  const client = clientFor(config);
  let fetched = 0;
  let syncError: string | undefined;
  try {
    await client.connect();
  } catch (e) {
    syncError = (e as Error).message;
    await updateImapState(config.id, {
      lastSyncAt: new Date(),
      lastSyncError: syncError,
    });
    return { fetched: 0, error: syncError };
  }

  try {
    const lock = await client.getMailboxLock("INBOX");
    try {
      const mailbox = client.mailbox;
      if (!mailbox || typeof mailbox === "boolean") {
        syncError = "Could not open INBOX";
      } else {
        const uidValidity = Number(mailbox.uidValidity ?? 0);
        const exists = mailbox.exists ?? 0;

        // A changed UIDVALIDITY means the server renumbered the mailbox — our
        // stored UID cursor is meaningless, so treat this like a first sync.
        const cursorValid =
          config.imapLastUid != null &&
          config.imapUidValidity != null &&
          config.imapUidValidity === uidValidity;
        const lastUid = cursorValid ? config.imapLastUid! : 0;

        if (exists === 0) {
          await updateImapState(config.id, { lastUid, uidValidity });
        } else {
          // Incremental: UID FETCH "lastUid+1:*" (filter the always-included
          // tail). First sync: most recent INITIAL_SYNC_LIMIT messages by seq.
          const useUid = cursorValid;
          const range = cursorValid
            ? `${lastUid + 1}:*`
            : `${Math.max(1, exists - INITIAL_SYNC_LIMIT + 1)}:${exists}`;

          let maxUid = lastUid;
          let minUid = Infinity;
          let examined = 0;
          const rows: InsertRow[] = [];
          const bounces = new Map<number, BounceResult>();

          for await (const msg of client.fetch(
            range,
            {
              uid: true,
              size: true,
              envelope: true,
              source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
              internalDate: true,
            },
            useUid ? { uid: true } : undefined,
          )) {
            const uid = msg.uid;
            if (cursorValid && uid <= lastUid) continue;
            // Stop before advancing past the per-run cap, so the remainder is
            // re-fetched next tick rather than skipped forever.
            if (examined >= MAX_PER_SYNC) break;
            examined += 1;
            // Advance across the processing window (including skipped messages)
            // so a single bad message can't wedge the cursor.
            if (uid > maxUid) maxUid = uid;
            if (!msg.source) continue;
            const row = await parseMessageToRow(
              config,
              uid,
              msg.source,
              msg.internalDate,
              bounces,
              msg.size,
              msg.envelope,
            );
            if (!row) continue;
            rows.push(row);
            if (uid < minUid) minUid = uid;
          }

          const inserted = rows.length ? await ingest(rows) : [];
          fetched = inserted.length;

          // Process permanent bounces among the genuinely-new messages: suppress
          // the dead recipient + mark the original send bounced. Only new mail
          // (not backfill) so we never act on ancient DSNs.
          for (const m of inserted) {
            const b = bounces.get(m.imapUid);
            if (b?.permanent) {
              // Await the transactional bounce/outbox commit. Fire-and-forget
              // here could lose a terminal event if the worker exited after
              // storing the IMAP row but before persisting its outbox intent.
              await processBounce(config.organizationId, b);
            }
          }

          // firstUid is the lowest UID we hold — the start point for backfill.
          // It only ever moves down (first sync sets it; backfill lowers it).
          const newFirst =
            minUid === Infinity
              ? undefined
              : config.imapFirstUid == null
                ? minUid
                : Math.min(config.imapFirstUid, minUid);

          await updateImapState(config.id, {
            lastUid: maxUid,
            uidValidity,
            ...(newFirst !== undefined ? { firstUid: newFirst } : {}),
          });

          // Fire the automation hook once per genuinely-new message (never for
          // re-ingests or backfill).
          for (const m of inserted) {
            await dispatchEvent(config.organizationId, "incoming.received", {
              id: m.id,
              smtpConfigId: config.id,
              from: m.fromAddress,
              fromName: m.fromName,
              subject: m.subject,
              receivedAt: m.receivedAt,
              sourceTruncated: m.sourceTruncated,
              contentTruncated: m.contentTruncated,
            });
          }
        }
      }
    } finally {
      lock.release();
    }
  } catch (e) {
    syncError = (e as Error).message;
  } finally {
    try {
      await client.logout();
    } catch {
      /* best-effort */
    }
  }

  await updateImapState(config.id, {
    lastSyncAt: new Date(),
    lastSyncError: syncError ?? null,
  });
  return { fetched, error: syncError };
}

/**
 * Pull a batch of OLDER mail (UIDs below firstUid) — progressive history
 * backfill. Does NOT fire webhooks (old mail isn't "received" now). Lowers
 * firstUid and flags backfill complete when it reaches the bottom.
 */
export async function backfillMailbox(
  config: SmtpConfig,
  batch = INITIAL_SYNC_LIMIT,
): Promise<SyncResult> {
  if (!config.imapHost || !config.imapPassword) return { fetched: 0 };
  if (config.imapBackfillComplete) return { fetched: 0 };
  // No baseline yet — a forward sync must establish firstUid first.
  if (config.imapFirstUid == null) return syncMailbox(config);

  const client = clientFor(config);
  let fetched = 0;
  let error: string | undefined;
  try {
    await client.connect();
  } catch (e) {
    return { fetched: 0, error: (e as Error).message };
  }

  try {
    const lock = await client.getMailboxLock("INBOX");
    try {
      const mailbox = client.mailbox;
      if (!mailbox || typeof mailbox === "boolean") {
        error = "Could not open INBOX";
      } else {
        const uidValidity = Number(mailbox.uidValidity ?? 0);
        if (
          config.imapUidValidity != null &&
          config.imapUidValidity !== uidValidity
        ) {
          // Mailbox renumbered — firstUid is meaningless; let a forward sync reset.
          error = "UIDVALIDITY changed; run a sync first";
        } else {
          const firstUid = config.imapFirstUid;
          const hi = firstUid - 1;
          if (hi < 1) {
            await updateImapState(config.id, { backfillComplete: true });
          } else {
            const safeBatch = Number.isFinite(batch)
              ? Math.min(MAX_PER_SYNC, Math.max(1, Math.trunc(batch)))
              : INITIAL_SYNC_LIMIT;
            const low = Math.max(1, firstUid - safeBatch);
            const rows: InsertRow[] = [];
            for await (const msg of client.fetch(
              `${low}:${hi}`,
              {
                uid: true,
                size: true,
                envelope: true,
                source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
                internalDate: true,
              },
              { uid: true },
            )) {
              const uid = msg.uid;
              if (uid >= firstUid) continue;
              if (!msg.source) continue;
              if (rows.length >= MAX_PER_SYNC) break;
              const row = await parseMessageToRow(
                config,
                uid,
                msg.source,
                msg.internalDate,
                undefined,
                msg.size,
                msg.envelope,
              );
              if (row) rows.push(row);
            }
            if (rows.length) fetched = (await ingest(rows)).length;
            // We've now covered [low, firstUid-1]; the new floor is `low`.
            await updateImapState(config.id, {
              firstUid: low,
              backfillComplete: low <= 1,
            });
          }
        }
      }
    } finally {
      lock.release();
    }
  } catch (e) {
    error = (e as Error).message;
  } finally {
    try {
      await client.logout();
    } catch {
      /* best-effort */
    }
  }

  return { fetched, error };
}

export interface FetchedAttachment {
  filename: string;
  contentType: string;
  content: Buffer;
}

export class IncomingEmailContentTooLargeError extends Error {
  constructor() {
    super("Incoming email exceeds the safe content limit");
    this.name = "IncomingEmailContentTooLargeError";
  }
}

/**
 * Re-fetch a single attachment on demand (we store only metadata, not blobs).
 * `index` is the position in the stored attachments array.
 */
export async function fetchAttachment(
  config: SmtpConfig,
  uid: number,
  index: number,
): Promise<FetchedAttachment | null> {
  if (!config.imapHost || !config.imapPassword) return null;
  const client = clientFor(config);
  await client.connect();
  try {
    const lock = await client.getMailboxLock("INBOX");
    try {
      const msg = await client.fetchOne(
        String(uid),
        {
          size: true,
          source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
        },
        { uid: true },
      );
      if (!msg || !msg.source) return null;
      if (
        (msg.size != null && msg.size > msg.source.length) ||
        (msg.size == null && msg.source.length >= INCOMING_SOURCE_MAX_BYTES)
      ) {
        throw new IncomingEmailContentTooLargeError();
      }
      const parsed = await simpleParser(msg.source, {
        skipHtmlToText: true,
        skipImageLinks: true,
      });
      const atts = (parsed.attachments ?? []).filter(
        (a) => a.filename || a.related === false,
      );
      const att = atts[index];
      if (!att) return null;
      if (att.content.length > INCOMING_ATTACHMENT_MAX_BYTES) {
        throw new IncomingEmailContentTooLargeError();
      }
      return {
        filename: boundString(
          att.filename ?? "attachment",
          INCOMING_FILENAME_MAX_CHARS,
        ).value,
        contentType: boundString(
          att.contentType ?? "application/octet-stream",
          INCOMING_CONTENT_TYPE_MAX_CHARS,
        ).value,
        content: att.content,
      };
    } finally {
      lock.release();
    }
  } finally {
    try {
      await client.logout();
    } catch {
      /* best-effort */
    }
  }
}

/** Quick credential check used by the SMTP form's "Test" affordance. */
export async function testImapConnection(
  config: Pick<
    SmtpConfig,
    "imapHost" | "imapPort" | "imapSecure" | "imapUsername" | "imapPassword" | "username"
  >,
): Promise<{ success: true } | { success: false; error: string }> {
  if (!config.imapHost) return { success: false, error: "IMAP host not set" };
  const client = new ImapFlow({
    host: config.imapHost,
    port: config.imapPort ?? 993,
    secure: config.imapSecure ?? true,
    auth: {
      user: config.imapUsername || config.username || "",
      pass: config.imapPassword ?? "",
    },
    logger: false,
    tls: { rejectUnauthorized: TLS_REJECT_UNAUTHORIZED },
    greetingTimeout: 15000,
    connectionTimeout: 15000,
    maxLiteralSize: INCOMING_SOURCE_MAX_BYTES,
    maxLineLength: INCOMING_PROTOCOL_MAX_LINE_BYTES,
  });
  try {
    await client.connect();
    await client.logout();
    return { success: true };
  } catch (e) {
    return { success: false, error: (e as Error).message };
  }
}
