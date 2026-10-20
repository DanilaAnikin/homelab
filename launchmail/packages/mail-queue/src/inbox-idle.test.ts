import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SmtpConfig } from "./smtp-configs.service";

interface FakeClient {
  options: { host: string; auth: { pass: string } };
  usable: boolean;
  closed: boolean;
  emit: (event: string, ...args: unknown[]) => boolean;
  listenerCount: (event: string) => number;
  close: () => void;
}

const h = vi.hoisted(() => ({
  configs: [] as SmtpConfig[],
  clients: [] as FakeClient[],
  // "<host>|<password>" combinations whose connect() fails.
  failConnect: new Set<string>(),
  syncResults: new Map<string, { fetched: number; error?: string }>(),
  // Runs at the start of every syncMailbox call (e.g. to move another
  // mailbox's stored cursor while this sync is running).
  onSync: null as ((config: SmtpConfig) => void) | null,
  // While set, every syncMailbox call waits for it (a long-running sync).
  syncGate: null as Promise<void> | null,
  // LOGOUT never answers; it only fails once the socket is closed.
  hangLogout: false,
}));

vi.mock("imapflow", async () => {
  const { EventEmitter } = await import("node:events");
  return {
    ImapFlow: class extends EventEmitter {
      usable = false;
      closed = false;
      constructor(public options: { host: string; auth: { pass: string } }) {
        super();
        h.clients.push(this as unknown as FakeClient);
      }
      async connect() {
        if (h.failConnect.has(`${this.options.host}|${this.options.auth.pass}`)) {
          throw Object.assign(new Error("Socket timeout"), { code: "ETIMEOUT" });
        }
        this.usable = true;
      }
      async mailboxOpen() {}
      idle() {
        if (!this.usable) return Promise.reject(new Error("Connection not available"));
        return new Promise<void>((resolve) => this.once("close", () => resolve()));
      }
      close() {
        if (this.closed) return;
        this.closed = true;
        this.usable = false;
        this.emit("close");
      }
      logout() {
        if (h.hangLogout) {
          return new Promise<void>((_resolve, reject) =>
            this.once("close", () => reject(new Error("Connection not available"))),
          );
        }
        this.close();
        return Promise.resolve();
      }
    },
  };
});

vi.mock("./smtp-configs.service", () => ({
  listReceiveEnabledConfigs: vi.fn(async () => h.configs.map((c) => ({ ...c }))),
  getSmtpConfigById: vi.fn(
    async (id: string) => {
      const c = h.configs.find((x) => x.id === id);
      return c ? { ...c } : null;
    },
  ),
}));

vi.mock("./imap.service", () => ({
  syncMailbox: vi.fn(async (c: SmtpConfig) => {
    h.onSync?.(c);
    if (h.syncGate) await h.syncGate;
    return h.syncResults.get(c.id) ?? { fetched: 0 };
  }),
  backfillMailbox: vi.fn(async () => ({ fetched: 0 })),
}));

import { startInboxIdle, type InboxIdleManager } from "./inbox-idle";
import { MailboxBackoff } from "./mailbox-backoff";
import { syncMailbox } from "./imap.service";

function mailbox(id: string, host: string, extra: Partial<SmtpConfig> = {}): SmtpConfig {
  return {
    id,
    organizationId: "org-1",
    name: id,
    username: `${id}@example.test`,
    imapHost: host,
    imapPort: 993,
    imapSecure: true,
    imapUsername: `${id}@example.test`,
    imapPassword: "app-password",
    imapLastUid: 10,
    imapUidValidity: 1,
    imapFirstUid: 1,
    imapBackfillComplete: true,
    ...extra,
  } as SmtpConfig;
}

const syncCallsFor = (id: string) =>
  vi.mocked(syncMailbox).mock.calls.filter(([c]) => c.id === id);

let manager: InboxIdleManager | null = null;

function start(baseMs = 30_000) {
  manager = startInboxIdle({
    reconcileMs: 60_000,
    pollMs: 20_000,
    bootDelayMs: 10,
    backoff: new MailboxBackoff({ baseMs, maxMs: 600_000, jitter: 0 }),
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  h.configs.length = 0;
  h.clients.length = 0;
  h.failConnect.clear();
  h.syncResults.clear();
  h.onSync = null;
  h.syncGate = null;
  h.hangLogout = false;
  vi.clearAllMocks();
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(async () => {
  await manager?.stop();
  manager = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("inbox-idle failure isolation", () => {
  it("backs off a failing mailbox while the others keep syncing, and resets on success", async () => {
    h.configs.push(mailbox("gmail", "imap.gmail.com"), mailbox("seznam", "imap.seznam.cz"));
    h.syncResults.set("gmail", { fetched: 0, error: "Socket timeout [ETIMEOUT]" });
    start();

    await vi.advanceTimersByTimeAsync(10 + 1_500); // boot reconcile + catch-up sync
    expect(syncCallsFor("gmail")).toHaveLength(1);
    expect(syncCallsFor("seznam")).toHaveLength(1);

    // Polls run every 20 s. First failure at 1.5 s → next attempt from 31.5 s.
    await vi.advanceTimersByTimeAsync(20_000); // poll @ 20 s: gmail skipped
    expect(syncCallsFor("gmail")).toHaveLength(1);
    expect(syncCallsFor("seznam")).toHaveLength(2);

    await vi.advanceTimersByTimeAsync(20_000); // poll @ 40 s: due, fails again → 60 s
    expect(syncCallsFor("gmail")).toHaveLength(2);
    expect(syncCallsFor("seznam")).toHaveLength(3);
    expect(console.error).toHaveBeenCalledWith(
      expect.stringContaining("sync gmail failed (2x in a row, next attempt in 60 s)"),
    );

    h.syncResults.delete("gmail"); // e.g. new app password / throttling over
    await vi.advanceTimersByTimeAsync(40_000); // polls @ 60 s and 80 s: still waiting
    expect(syncCallsFor("gmail")).toHaveLength(2);
    expect(syncCallsFor("seznam")).toHaveLength(5);

    await vi.advanceTimersByTimeAsync(20_000); // poll @ 100 s: due, succeeds
    expect(syncCallsFor("gmail")).toHaveLength(3);
    expect(console.log).toHaveBeenCalledWith(
      "[inbox-idle] sync gmail recovered after 2 failed attempt(s)",
    );

    await vi.advanceTimersByTimeAsync(20_000); // healthy again → every poll
    expect(syncCallsFor("gmail")).toHaveLength(4);
  });

  it("survives an 'error' event on the IDLE connection and reconnects on the next reconcile", async () => {
    h.configs.push(mailbox("gmail", "imap.gmail.com"));
    start();
    await vi.advanceTimersByTimeAsync(10);
    expect(h.clients).toHaveLength(1);
    const first = h.clients[0]!;
    expect(first.listenerCount("error")).toBeGreaterThan(0);

    // What imapflow does on a socket timeout: emit 'error', then close.
    expect(() =>
      first.emit("error", Object.assign(new Error("Socket timeout"), { code: "ETIMEOUT" })),
    ).not.toThrow();
    first.close();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.clients).toHaveLength(2);
    expect(h.clients[1]!.usable).toBe(true);
  });

  it("syncs from the CURRENT cursor when the server pushes new mail", async () => {
    h.configs.push(mailbox("seznam", "imap.seznam.cz", { imapLastUid: 10 }));
    start();
    await vi.advanceTimersByTimeAsync(10 + 1_500);
    expect(syncCallsFor("seznam").at(-1)?.[0].imapLastUid).toBe(10);

    // A sync elsewhere advanced the stored cursor; then the server pushes EXISTS.
    h.configs[0] = { ...h.configs[0]!, imapLastUid: 75 };
    h.clients[0]!.emit("exists", { count: 76, prevCount: 75 });
    await vi.advanceTimersByTimeAsync(1_500);

    expect(syncCallsFor("seznam").at(-1)?.[0].imapLastUid).toBe(75);
  });

  it("backs off IDLE reconnects of a failing mailbox and retries at once with new credentials", async () => {
    h.configs.push(mailbox("gmail", "imap.gmail.com"));
    h.failConnect.add("imap.gmail.com|app-password");
    start(90_000);

    await vi.advanceTimersByTimeAsync(10);
    expect(h.clients).toHaveLength(1);
    expect(h.clients[0]!.closed).toBe(true); // failed connect is closed explicitly

    await vi.advanceTimersByTimeAsync(60_000); // reconcile @ 60 s: still within 90 s
    expect(h.clients).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000); // reconcile @ 120 s: due → retried
    expect(h.clients).toHaveLength(2);

    h.configs[0] = { ...h.configs[0]!, imapPassword: "new-app-password" };
    await vi.advanceTimersByTimeAsync(60_000); // backoff (180 s) reset by new creds
    expect(h.clients).toHaveLength(3);
    expect(h.clients[2]!.usable).toBe(true);
  });
  it("safety poll syncs each mailbox from its CURRENT cursor, not from the poll's snapshot", async () => {
    h.configs.push(
      mailbox("first", "imap.seznam.cz", { imapLastUid: 10 }),
      mailbox("second", "imap.seznam.cz", { imapLastUid: 10 }),
    );
    start();
    await vi.advanceTimersByTimeAsync(10 + 1_500); // boot + catch-up syncs
    expect(syncCallsFor("second")).toHaveLength(1);

    // While the poll is busy with "first", an IDLE sync of "second" moves its
    // stored cursor to 75. The poll read both rows (cursor 10) when it started.
    h.onSync = (c) => {
      if (c.id === "first") {
        h.configs[1] = { ...h.configs[1]!, imapLastUid: 75 };
      }
    };
    await vi.advanceTimersByTimeAsync(20_000); // safety poll @ 20 s

    expect(syncCallsFor("first")).toHaveLength(2);
    expect(syncCallsFor("second")).toHaveLength(2);
    // Syncing from the snapshot (10) re-downloaded up to 50 messages and
    // wrote the cursor back from 75 to wherever that batch ended.
    expect(syncCallsFor("second").at(-1)?.[0].imapLastUid).toBe(75);
  });

  it("a push during a running sync is not dropped: exactly one more sync follows", async () => {
    h.configs.push(mailbox("seznam", "imap.seznam.cz"));
    let release!: () => void;
    h.syncGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    start();
    await vi.advanceTimersByTimeAsync(10 + 1_500); // catch-up sync starts and hangs
    expect(syncCallsFor("seznam")).toHaveLength(1);

    // New mail arrives (three pushes) while that sync is still running.
    const client = h.clients[0]!;
    client.emit("exists", { count: 11, prevCount: 10 });
    client.emit("exists", { count: 12, prevCount: 11 });
    await vi.advanceTimersByTimeAsync(1_500); // debounced runSync: busy → pending
    client.emit("exists", { count: 13, prevCount: 12 });
    await vi.advanceTimersByTimeAsync(1_500);
    expect(syncCallsFor("seznam")).toHaveLength(1);

    h.syncGate = null;
    release();
    await vi.advanceTimersByTimeAsync(1_500); // follow-up sync after the debounce
    expect(syncCallsFor("seznam")).toHaveLength(2);

    await vi.advanceTimersByTimeAsync(10_000); // still before the 20 s poll
    expect(syncCallsFor("seznam")).toHaveLength(2);
  });

  it("does not schedule the follow-up sync while the mailbox is in backoff", async () => {
    h.configs.push(mailbox("gmail", "imap.gmail.com"));
    h.syncResults.set("gmail", { fetched: 0, error: "Socket timeout [ETIMEOUT]" });
    let release!: () => void;
    h.syncGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    start();
    await vi.advanceTimersByTimeAsync(10 + 1_500);
    h.clients[0]!.emit("exists", { count: 11, prevCount: 10 });
    await vi.advanceTimersByTimeAsync(1_500); // busy → pending
    h.syncGate = null;
    release(); // the running sync fails → backoff 30 s
    await vi.advanceTimersByTimeAsync(5_000);
    expect(syncCallsFor("gmail")).toHaveLength(1);
  });

  it("stop() waits for an unanswered LOGOUT at most 2 s, then closes the socket", async () => {
    h.configs.push(mailbox("gmail", "imap.gmail.com"), mailbox("seznam", "imap.seznam.cz"));
    start();
    await vi.advanceTimersByTimeAsync(10);
    expect(h.clients).toHaveLength(2);
    h.hangLogout = true;

    let stopped = false;
    const stopping = manager!.stop().then(() => {
      stopped = true;
    });
    manager = null;
    await vi.advanceTimersByTimeAsync(1_999);
    expect(stopped).toBe(false);
    expect(h.clients.every((c) => !c.closed)).toBe(true);

    await vi.advanceTimersByTimeAsync(1); // both limits run in parallel
    await stopping;
    expect(stopped).toBe(true);
    expect(h.clients.every((c) => c.closed)).toBe(true);
  });
});
