import { ImapFlow } from "imapflow";
import {
  listReceiveEnabledConfigs,
  getSmtpConfigById,
  type SmtpConfig,
} from "./smtp-configs.service";
import { syncMailbox, backfillMailbox } from "./imap.service";

const TLS_REJECT_UNAUTHORIZED =
  process.env.INBOX_IMAP_REJECT_UNAUTHORIZED !== "false";

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

interface Conn {
  client: ImapFlow;
  fingerprint: string;
  syncing: boolean;
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
 */
export function startInboxIdle(opts?: {
  reconcileMs?: number;
  pollMs?: number;
}): InboxIdleManager {
  const reconcileMs = opts?.reconcileMs ?? 60_000;
  const pollMs =
    opts?.pollMs ??
    Number(process.env.INBOX_POLL_INTERVAL_MS ?? 300_000);

  const conns = new Map<string, Conn>();
  // Serializes syncs for a mailbox across BOTH the IDLE path and the safety
  // poll, so the same message can never be ingested by two overlapping syncs
  // (which—though the unique index would still dedupe the row—keeps work and
  // any side effects single-flighted per mailbox).
  const inFlight = new Set<string>();
  let stopped = false;
  let reconciling = false;
  let polling = false;

  async function runSync(config: SmtpConfig, conn: Conn) {
    if (conn.syncing || inFlight.has(config.id) || stopped) return;
    conn.syncing = true;
    inFlight.add(config.id);
    try {
      const r = await syncMailbox(config);
      if (r.fetched > 0) {
        console.log(`[inbox-idle] ${config.name}: +${r.fetched} new`);
      }
    } catch (e) {
      console.error(
        `[inbox-idle] sync ${config.name} failed: ${(e as Error).message}`,
      );
    } finally {
      conn.syncing = false;
      inFlight.delete(config.id);
    }
  }

  function scheduleSync(config: SmtpConfig, conn: Conn) {
    if (conn.debounce) clearTimeout(conn.debounce);
    // Coalesce bursts of `exists` events into one sync.
    conn.debounce = setTimeout(() => void runSync(config, conn), 1500);
  }

  async function open(config: SmtpConfig) {
    const client = new ImapFlow({
      host: config.imapHost!,
      port: config.imapPort ?? 993,
      secure: config.imapSecure ?? true,
      auth: {
        user: config.imapUsername || config.username,
        pass: config.imapPassword ?? "",
      },
      logger: false,
      tls: { rejectUnauthorized: TLS_REJECT_UNAUTHORIZED },
      greetingTimeout: 15000,
      connectionTimeout: 15000,
    });
    const conn: Conn = {
      client,
      fingerprint: fingerprint(config),
      syncing: false,
      debounce: null,
    };

    const drop = () => {
      if (conns.get(config.id) === conn) conns.delete(config.id);
      if (conn.debounce) clearTimeout(conn.debounce);
    };
    client.on("error", drop);
    client.on("close", drop);
    // The server pushes `exists` when message count grows — new mail arrived.
    client.on("exists", () => scheduleSync(config, conn));

    try {
      await client.connect();
      await client.mailboxOpen("INBOX");
      conns.set(config.id, conn);
      // Catch up on anything that arrived while we were disconnected.
      scheduleSync(config, conn);
      console.log(`[inbox-idle] watching ${config.name} (${config.imapHost})`);
      // imapflow does not reliably auto-IDLE — keep an explicit IDLE loop alive
      // so the server pushes `exists` the instant mail arrives. The fetch runs
      // on a SEPARATE connection (syncMailbox), so it never interrupts IDLE.
      void (async () => {
        while (!stopped && conns.get(config.id) === conn && client.usable) {
          try {
            await client.idle();
          } catch {
            break;
          }
        }
      })();
    } catch (e) {
      drop();
      try {
        await client.logout();
      } catch {
        /* ignore */
      }
      // syncMailbox (run by the safety poll) records the health/error on the
      // config so the UI surfaces it; here we just log and let reconcile retry.
      console.warn(
        `[inbox-idle] connect ${config.name} failed: ${(e as Error).message}`,
      );
    }
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
      // Close connections whose config was removed or whose creds changed.
      for (const [id, conn] of [...conns]) {
        const c = byId.get(id);
        if (!c || fingerprint(c) !== conn.fingerprint) {
          conns.delete(id);
          if (conn.debounce) clearTimeout(conn.debounce);
          try {
            await conn.client.logout();
          } catch {
            /* ignore */
          }
        }
      }
      // Open connections for new / dropped mailboxes.
      for (const c of configs) {
        if (stopped) break;
        if (!conns.has(c.id)) await open(c);
      }
    } catch (e) {
      console.error(`[inbox-idle] reconcile failed: ${(e as Error).message}`);
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
        inFlight.add(c.id);
        try {
          await syncMailbox(c);
          // Advance history backfill one batch per cycle. Re-read so we use the
          // firstUid just updated by the sync above.
          if (!c.imapBackfillComplete) {
            const fresh = await getSmtpConfigById(c.id);
            if (fresh && !fresh.imapBackfillComplete) {
              await backfillMailbox(fresh);
            }
          }
        } catch (e) {
          console.error(
            `[inbox-idle] poll ${c.name} failed: ${(e as Error).message}`,
          );
        } finally {
          inFlight.delete(c.id);
        }
      }
    } catch (e) {
      console.error(`[inbox-idle] safety poll failed: ${(e as Error).message}`);
    } finally {
      clearTimeout(guard);
      polling = false;
    }
  }

  // Boot: reconcile soon, then keep connections + safety poll on their timers.
  const bootTimer = setTimeout(() => void reconcile(), 8_000);
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
      // Await all logouts so the process doesn't exit mid-handshake and leave
      // half-open IMAP sessions on the server.
      await Promise.all(
        Array.from(conns.values()).map((conn) => {
          if (conn.debounce) clearTimeout(conn.debounce);
          return conn.client.logout().catch(() => undefined);
        }),
      );
      conns.clear();
    },
  };
}
