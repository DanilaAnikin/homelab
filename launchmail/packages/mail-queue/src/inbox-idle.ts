import type { ImapFlow } from "imapflow";
import {
  listReceiveEnabledConfigs,
  getSmtpConfigById,
  type SmtpConfig,
} from "./smtp-configs.service";
import { syncMailbox, backfillMailbox } from "./imap.service";
import {
  INCOMING_PROTOCOL_MAX_LINE_BYTES,
  INCOMING_SOURCE_MAX_BYTES,
} from "./incoming-email-limits";
import { createImapClient } from "./imap-client";
import { MailboxBackoff } from "./mailbox-backoff";
import { describeError } from "./safe-error";

const TLS_REJECT_UNAUTHORIZED =
  process.env.INBOX_IMAP_REJECT_UNAUTHORIZED !== "false";

// A mailbox whose sync or IDLE connect fails is retried after ~30 s, 1, 2, 4,
// 8 min and then every ~10 min until it succeeds; success resets the streak.
// The safety poll still covers every other mailbox on its normal schedule.
const RETRY_BASE_MS = 30_000;
const RETRY_MAX_MS = 10 * 60_000;

// stop(): how long one IDLE connection may take to LOGOUT before its socket is
// simply closed. A slow server (Gmail took 37 s to log in on 20. 10. 2026)
// must not eat the worker's shutdown deadline, which the BullMQ send jobs need.
const LOGOUT_LIMIT_MS = 2_000;

// Identity of the IMAP connection — when any of these change, the persistent
// connection is torn down and reopened with fresh credentials.
function fingerprint(c: SmtpConfig): string {
  return [
    c.imapHost,
    c.imapPort ?? 993,
    c.imapSecure ?? true,
    c.imapUsername || c.username,
    c.imapPassword ?? "",
  ].join("|");
}

function closeQuietly(client: ImapFlow): void {
  try {
    client.close();
  } catch {
    /* already closed */
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Resolves when `promise` settles or after `ms`, whichever comes first. */
async function settleWithin(promise: Promise<unknown>, ms: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      promise.then(
        () => undefined,
        () => undefined,
      ),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

interface Conn {
  client: ImapFlow;
  /** The config this connection was opened with (sync re-reads the row). */
  config: SmtpConfig;
  fingerprint: string;
  syncing: boolean;
  /** An `exists` push arrived while this mailbox was already syncing. */
  pending: boolean;
  debounce: ReturnType<typeof setTimeout> | null;
}

export interface InboxIdleManager {
  stop: () => Promise<void>;
}

/**
 * Real-time inbox via IMAP IDLE. Holds one persistent connection per
 * receive-enabled mailbox, opens INBOX, and on the server's `exists` push
 * triggers a (debounced) sync — so new mail lands in seconds, not minutes.
 *
 * A reconcile loop opens/closes/reconnects connections as configs change, and a
 * slower safety poll force-syncs every mailbox (covering missed pushes / servers
 * without IDLE) and advances history backfill one batch at a time. Runs in the
 * worker process.
 *
 * Failure isolation: every IMAP client has an 'error' listener (see
 * imap-client.ts), every timer/IDLE path catches its own errors, and a
 * mailbox that keeps failing is backed off on its own — it can neither crash
 * the process nor delay the other mailboxes.
 */
export function startInboxIdle(opts?: {
  reconcileMs?: number;
  pollMs?: number;
  bootDelayMs?: number;
  backoff?: MailboxBackoff;
}): InboxIdleManager {
  const reconcileMs = opts?.reconcileMs ?? 60_000;
  const pollMs =
    opts?.pollMs ??
    Number(process.env.INBOX_POLL_INTERVAL_MS ?? 300_000);
  const bootDelayMs = opts?.bootDelayMs ?? 8_000;
  const backoff =
    opts?.backoff ??
    new MailboxBackoff({ baseMs: RETRY_BASE_MS, maxMs: RETRY_MAX_MS });

  const conns = new Map<string, Conn>();
  // Mailboxes with an IDLE connect in progress (a slow server must not get a
  // second, parallel connection from the next reconcile tick).
  const opening = new Set<string>();
  // Last seen credentials per mailbox: a change (e.g. a new app password)
  // clears that mailbox's backoff so it is retried right away.
  const knownFingerprints = new Map<string, string>();
  // Serializes syncs for a mailbox across BOTH the IDLE path and the safety
  // poll, so the same message can never be ingested by two overlapping syncs
  // (which—though the unique index would still dedupe the row—keeps work and
  // any side effects single-flighted per mailbox).
  const inFlight = new Set<string>();
  let stopped = false;
  let reconciling = false;
  let polling = false;

  const syncKey = (id: string) => `sync:${id}`;
  const idleKey = (id: string) => `idle:${id}`;

  function noteFailure(key: string, what: string, name: string, error: string) {
    const { failures, delayMs } = backoff.recordFailure(key);
    console.error(
      `[inbox-idle] ${what} ${name} failed (${failures}x in a row, next attempt in ${Math.round(delayMs / 1000)} s): ${error}`,
    );
  }

  function noteSuccess(key: string, what: string, name: string) {
    const previous = backoff.recordSuccess(key);
    if (previous > 0) {
      console.log(
        `[inbox-idle] ${what} ${name} recovered after ${previous} failed attempt(s)`,
      );
    }
  }

  /** One sync of one mailbox; never throws. Returns whether it succeeded. */
  async function syncOnce(config: SmtpConfig): Promise<boolean> {
    try {
      const r = await syncMailbox(config);
      if (r.error) {
        noteFailure(syncKey(config.id), "sync", config.name, r.error);
        return false;
      }
      noteSuccess(syncKey(config.id), "sync", config.name);
      if (r.fetched > 0) {
        console.log(`[inbox-idle] ${config.name}: +${r.fetched} new`);
      }
      return true;
    } catch (e) {
      noteFailure(syncKey(config.id), "sync", config.name, describeError(e));
      return false;
    }
  }

  async function runSync(config: SmtpConfig, conn: Conn) {
    if (stopped) return;
    if (conn.syncing || inFlight.has(config.id)) {
      // The running sync may have listed the mailbox's UIDs before this push;
      // remember it so one more sync follows instead of dropping the push.
      conn.pending = true;
      return;
    }
    // A mailbox in backoff waits; the safety poll retries it once due.
    if (!backoff.isReady(syncKey(config.id))) return;
    conn.syncing = true;
    conn.pending = false;
    inFlight.add(config.id);
    try {
      // Re-read the row: the config captured when this connection opened
      // carries the UID cursor of THAT moment. Syncing from it re-fetched
      // everything since the connection opened and, past MAX_PER_SYNC, moved
      // the stored cursor backwards.
      const fresh = await getSmtpConfigById(config.id);
      if (fresh?.imapHost) await syncOnce(fresh);
    } catch (e) {
      noteFailure(syncKey(config.id), "sync", config.name, describeError(e));
    } finally {
      conn.syncing = false;
      inFlight.delete(config.id);
      followUpPending(config.id);
    }
  }

  /**
   * After a sync of `id` (IDLE path or safety poll): if a push arrived while
   * it ran, schedule exactly one more sync — unless the mailbox is in backoff,
   * then the safety poll retries it once due.
   */
  function followUpPending(id: string) {
    const conn = conns.get(id);
    if (!conn?.pending || stopped) return;
    conn.pending = false;
    if (!backoff.isReady(syncKey(id))) return;
    scheduleSync(conn.config, conn);
  }

  function scheduleSync(config: SmtpConfig, conn: Conn) {
    if (conn.debounce) clearTimeout(conn.debounce);
    // Coalesce bursts of `exists` events into one sync.
    conn.debounce = setTimeout(() => void runSync(config, conn), 1500);
  }

  function forget(id: string, conn: Conn) {
    if (conns.get(id) === conn) conns.delete(id);
    if (conn.debounce) {
      clearTimeout(conn.debounce);
      conn.debounce = null;
    }
  }

  /** Keep IDLE running on one connection; drop the connection when it ends. */
  async function idleLoop(config: SmtpConfig, conn: Conn) {
    const { client } = conn;
    try {
      while (!stopped && conns.get(config.id) === conn && client.usable) {
        const startedAt = Date.now();
        await client.idle();
        // idle() resolves at once when imapflow is already idling on this
        // connection; never let that become a busy loop.
        if (Date.now() - startedAt < 1000) await sleep(1000);
      }
    } catch (e) {
      if (!stopped) {
        console.warn(`[inbox-idle] IDLE ${config.name} ended: ${describeError(e)}`);
      }
    }
    // A connection without IDLE would sit in `conns` and silently stop
    // delivering pushes; drop it so the next reconcile opens a fresh one.
    if (!stopped && conns.get(config.id) === conn) {
      forget(config.id, conn);
      closeQuietly(client);
    }
  }

  async function open(config: SmtpConfig) {
    let conn: Conn | null = null;
    const onDrop = () => {
      if (conn) forget(config.id, conn);
    };
    const client = createImapClient(
      {
        host: config.imapHost!,
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
      },
      `${config.name} (IDLE)`,
      onDrop,
    );
    const opened: Conn = {
      client,
      config,
      fingerprint: fingerprint(config),
      syncing: false,
      pending: false,
      debounce: null,
    };
    conn = opened;
    client.on("close", onDrop);
    // The server pushes `exists` when message count grows — new mail arrived.
    client.on("exists", () => scheduleSync(config, opened));

    try {
      await client.connect();
      await client.mailboxOpen("INBOX");
    } catch (e) {
      onDrop();
      closeQuietly(client);
      // syncMailbox records the health/error on the config so the UI surfaces
      // it; here we log, back off and let a later reconcile retry.
      noteFailure(idleKey(config.id), "connect", config.name, describeError(e));
      return;
    }
    if (stopped) {
      closeQuietly(client);
      return;
    }
    noteSuccess(idleKey(config.id), "connect", config.name);
    conns.set(config.id, opened);
    // Catch up on anything that arrived while we were disconnected.
    scheduleSync(config, opened);
    console.log(`[inbox-idle] watching ${config.name} (${config.imapHost})`);
    // imapflow does not reliably auto-IDLE — keep an explicit IDLE loop alive
    // so the server pushes `exists` the instant mail arrives. The fetch runs
    // on a SEPARATE connection (syncMailbox), so it never interrupts IDLE.
    void idleLoop(config, opened);
  }

  async function reconcile() {
    if (reconciling || stopped) return;
    reconciling = true;
    // Never let a hung query (e.g. a stalled DB connection) wedge the guard
    // forever — force-release it so a later tick can recover.
    const guard = setTimeout(() => {
      reconciling = false;
    }, 45_000);
    try {
      const configs = await listReceiveEnabledConfigs();
      const byId = new Map(configs.map((c) => [c.id, c]));
      // New credentials (or a removed mailbox) reset that mailbox's backoff.
      for (const c of configs) {
        const fp = fingerprint(c);
        const known = knownFingerprints.get(c.id);
        if (known !== undefined && known !== fp) {
          backoff.reset(idleKey(c.id));
          backoff.reset(syncKey(c.id));
        }
        knownFingerprints.set(c.id, fp);
      }
      for (const id of [...knownFingerprints.keys()]) {
        if (!byId.has(id)) {
          knownFingerprints.delete(id);
          backoff.reset(idleKey(id));
          backoff.reset(syncKey(id));
        }
      }
      // Close connections whose config was removed or whose creds changed.
      for (const [id, conn] of [...conns]) {
        const c = byId.get(id);
        if (!c || fingerprint(c) !== conn.fingerprint) {
          forget(id, conn);
          // Not awaited: an unresponsive server must not stall the reconcile.
          void conn.client.logout().catch(() => closeQuietly(conn.client));
        }
      }
      // Open connections for new / dropped mailboxes.
      for (const c of configs) {
        if (stopped) break;
        if (conns.has(c.id) || opening.has(c.id)) continue;
        if (!backoff.isReady(idleKey(c.id))) continue;
        opening.add(c.id);
        try {
          await open(c);
        } finally {
          opening.delete(c.id);
        }
      }
    } catch (e) {
      console.error(`[inbox-idle] reconcile failed: ${describeError(e)}`);
    } finally {
      clearTimeout(guard);
      reconciling = false;
    }
  }

  async function safetyPoll() {
    if (polling || stopped) return;
    polling = true;
    const guard = setTimeout(() => {
      polling = false;
    }, 600_000);
    try {
      const configs = await listReceiveEnabledConfigs();
      for (const c of configs) {
        if (stopped) break;
        // Don't double-sync a mailbox the IDLE path is already handling.
        if (inFlight.has(c.id)) continue;
        if (!backoff.isReady(syncKey(c.id))) continue;
        inFlight.add(c.id);
        try {
          // Same as runSync: `configs` was read when this poll started, and
          // the poll walks every mailbox one after another. An IDLE sync may
          // have advanced this mailbox's cursor meanwhile; syncing from the
          // snapshot re-downloaded up to MAX_PER_SYNC messages and wrote the
          // cursor back to where it was.
          const fresh = await getSmtpConfigById(c.id);
          if (!fresh?.imapHost) continue;
          const ok = await syncOnce(fresh);
          // Advance history backfill one batch per cycle, only while the
          // mailbox is healthy. Re-read so we use the firstUid just updated
          // by the sync above.
          if (ok && !fresh.imapBackfillComplete) {
            const after = await getSmtpConfigById(c.id);
            if (after && !after.imapBackfillComplete) {
              const b = await backfillMailbox(after);
              if (b.error) {
                console.warn(`[inbox-idle] backfill ${c.name} failed: ${b.error}`);
              }
            }
          }
        } catch (e) {
          console.error(`[inbox-idle] poll ${c.name} failed: ${describeError(e)}`);
        } finally {
          inFlight.delete(c.id);
          followUpPending(c.id);
        }
      }
    } catch (e) {
      console.error(`[inbox-idle] safety poll failed: ${describeError(e)}`);
    } finally {
      clearTimeout(guard);
      polling = false;
    }
  }

  // Boot: reconcile soon, then keep connections + safety poll on their timers.
  const bootTimer = setTimeout(() => void reconcile(), bootDelayMs);
  const reconcileTimer = setInterval(() => void reconcile(), reconcileMs);
  const pollTimer = setInterval(() => void safetyPoll(), pollMs);
  console.log(
    `[inbox-idle] started (IDLE push; reconcile ${Math.round(
      reconcileMs / 1000,
    )}s, safety poll ${Math.round(pollMs / 1000)}s)`,
  );

  return {
    async stop() {
      stopped = true;
      clearTimeout(bootTimer);
      clearInterval(reconcileTimer);
      clearInterval(pollTimer);
      const openConns = Array.from(conns.values());
      conns.clear();
      // LOGOUT politely so the server doesn't keep half-open sessions, but
      // only for LOGOUT_LIMIT_MS: all connections in parallel, then the socket
      // is closed regardless (a no-op after a clean LOGOUT).
      await Promise.all(
        openConns.map(async (conn) => {
          if (conn.debounce) {
            clearTimeout(conn.debounce);
            conn.debounce = null;
          }
          await settleWithin(
            Promise.resolve().then(() => conn.client.logout()),
            LOGOUT_LIMIT_MS,
          );
          closeQuietly(conn.client);
        }),
      );
    },
  };
}
