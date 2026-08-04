import { ImapFlow, type ImapFlowOptions } from "imapflow";
import { simpleParser, type AddressObject, type ParsedMail } from "mailparser";
import { db } from "@workspace/db";
import { incomingEmails } from "@workspace/db/schemas";
import type { SmtpConfig } from "./smtp-configs.service";
import { updateImapState } from "./smtp-configs.service";
import { dispatchEvent } from "./webhooks.service";
import { parseBounce, type BounceResult } from "./bounce";
import { processBounce } from "./bounce-handler";

// On the very first sync of a mailbox we pull the most recent N messages so the
// inbox is useful immediately without dragging in years of history in one shot.
// Subsequent polls only fetch UIDs above the stored cursor, so the rest of the
// archive is left alone unless explicitly backfilled later.
const INITIAL_SYNC_LIMIT = 200;
// Hard cap on messages ingested in a single poll, so a mailbox that received a
// huge burst can't make one sync run unbounded. The remainder lands next tick.
const MAX_PER_SYNC = 300;

type Addr = { email: string; name?: string };

function toAddrs(obj?: AddressObject | AddressObject[]): Addr[] {
  if (!obj) return [];
  const list = Array.isArray(obj) ? obj : [obj];
  const out: Addr[] = [];
  for (const o of list) {
    for (const a of o.value ?? []) {
      if (!a.address) continue;
      out.push(a.name ? { email: a.address, name: a.name } : { email: a.address });
    }
  }
  return out;
}

function buildSnippet(parsed: ParsedMail): string {
  const base = (parsed.text ?? parsed.subject ?? "").replace(/\s+/g, " ").trim();
  return base.slice(0, 240);
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
): Promise<InsertRow | null> {
  let parsed: ParsedMail;
  try {
    parsed = await simpleParser(source);
  } catch {
    return null;
  }
  // Bounce detection: collect DSNs so the caller can suppress dead recipients
  // after ingest. The message is still stored normally (users see the bounce).
  if (bounces) {
    const b = parseBounce(parsed);
    if (b.isBounce) bounces.set(uid, b);
  }
  const from = toAddrs(parsed.from)[0];
  const attachments = (parsed.attachments ?? [])
    .filter((a) => a.filename || a.related === false)
    .map((a) => ({
      filename: a.filename ?? "attachment",
      contentType: a.contentType ?? "application/octet-stream",
      size: a.size ?? 0,
    }));

  return {
    organizationId: config.organizationId,
    smtpConfigId: config.id,
    imapUid: uid,
    messageId: parsed.messageId ?? null,
    inReplyTo: parsed.inReplyTo ?? null,
    references: Array.isArray(parsed.references)
      ? parsed.references.join(" ")
      : (parsed.references ?? null),
    fromAddress: from?.email ?? "unknown@unknown",
    fromName: from?.name ?? null,
    toAddresses: toAddrs(parsed.to),
    ccAddresses: toAddrs(parsed.cc),
    subject: parsed.subject ?? null,
    snippet: buildSnippet(parsed),
    text: parsed.text ?? null,
    html: parsed.html === false ? null : (parsed.html ?? null),
    hasAttachments: attachments.length > 0,
    attachments: attachments.length > 0 ? attachments : null,
    receivedAt: new Date(parsed.date ?? internalDate ?? Date.now()),
  };
}

interface InsertedRow {
  id: string;
  imapUid: number;
  fromAddress: string;
  fromName: string | null;
  subject: string | null;
  receivedAt: Date;
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
          const rows: InsertRow[] = [];
          const bounces = new Map<number, BounceResult>();

          for await (const msg of client.fetch(
            range,
            { uid: true, source: true, internalDate: true },
            useUid ? { uid: true } : undefined,
          )) {
            const uid = msg.uid;
            if (cursorValid && uid <= lastUid) continue;
            // Stop before advancing past the per-run cap, so the remainder is
            // re-fetched next tick rather than skipped forever.
            if (rows.length >= MAX_PER_SYNC) break;
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
            const low = Math.max(1, firstUid - batch);
            const rows: InsertRow[] = [];
            for await (const msg of client.fetch(
              `${low}:${hi}`,
              { uid: true, source: true, internalDate: true },
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
      const msg = await client.fetchOne(String(uid), { source: true }, { uid: true });
      if (!msg || !msg.source) return null;
      const parsed = await simpleParser(msg.source);
      const atts = (parsed.attachments ?? []).filter(
        (a) => a.filename || a.related === false,
      );
      const att = atts[index];
      if (!att) return null;
      return {
        filename: att.filename ?? "attachment",
        contentType: att.contentType ?? "application/octet-stream",
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
  });
  try {
    await client.connect();
    await client.logout();
    return { success: true };
  } catch (e) {
    return { success: false, error: (e as Error).message };
  }
}
