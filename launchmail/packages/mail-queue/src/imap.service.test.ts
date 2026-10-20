import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SmtpConfig } from "./smtp-configs.service";

type Row = Record<string, unknown>;

const state = vi.hoisted(() => ({
  clientOptions: [] as Array<Record<string, unknown>>,
  clients: [] as Array<{
    listenerCount: (event: string) => number;
    emit: (event: string, ...args: unknown[]) => boolean;
    closed: boolean;
  }>,
  fetchCalls: [] as Array<{
    range: string;
    query: Record<string, unknown>;
    options: Record<string, unknown> | undefined;
  }>,
  fetchOneCalls: [] as Array<{
    range: string;
    query: Record<string, unknown>;
    options: Record<string, unknown> | undefined;
  }>,
  messages: [] as Array<Record<string, unknown>>,
  fetchOneMessage: null as Record<string, unknown> | null,
  insertedRows: [] as Array<Record<string, unknown>>,
  insertAttempts: 0,
  mailbox: { uidValidity: 42, exists: 1 },
  connectError: null as Error | null,
  // Emitted on the client right after connect() rejected — what imapflow's
  // orphaned startSession does ("Connection not available").
  lateError: null as Error | null,
  // Simulated database verdict for one INSERT statement (all-or-nothing).
  rejectInsert: null as ((rows: Row[]) => unknown) | null,
  // (smtp_config_id, imap_uid) pairs already stored — the unique index that
  // makes re-ingesting a message a no-op (ON CONFLICT DO NOTHING).
  storedKeys: new Set<string>(),
  // UIDs the source FETCH leaves out although the UID listing showed them.
  omitFromSource: new Set<number>(),
}));

const serviceMocks = vi.hoisted(() => ({
  updateImapState: vi.fn(),
  dispatchEvent: vi.fn(),
  processBounce: vi.fn(),
}));

/** imapflow-like sequence-set matching: "a", "a:b", "a:*" by UID or seq. */
function inRange(range: string, value: number, max: number): boolean {
  return range.split(",").some((part) => {
    const [lo, hi] = part.split(":");
    const low = lo === "*" ? max : Number(lo);
    if (hi === undefined) return value === low;
    const high = hi === "*" ? max : Number(hi);
    return value >= Math.min(low, high) && value <= Math.max(low, high);
  });
}

vi.mock("imapflow", async () => {
  const { EventEmitter } = await import("node:events");
  return {
    ImapFlow: class extends EventEmitter {
      mailbox = state.mailbox;
      closed = false;

      constructor(options: Record<string, unknown>) {
        super();
        state.clientOptions.push(options);
        state.clients.push(this);
      }

      async connect() {
        if (state.connectError) {
          const late = state.lateError;
          if (late) setImmediate(() => this.emit("error", late));
          throw state.connectError;
        }
      }

      close() {
        this.closed = true;
      }

      async logout() {}

      async getMailboxLock() {
        return { release: vi.fn() };
      }

      async *fetch(
        range: string,
        query: Record<string, unknown>,
        options?: Record<string, unknown>,
      ) {
        state.fetchCalls.push({ range, query, options });
        const byUid = Boolean(options?.uid);
        const maxUid = Math.max(0, ...state.messages.map((m) => Number(m.uid)));
        const maxSeq = state.messages.length;
        for (const [index, message] of state.messages.entries()) {
          const key = byUid ? Number(message.uid) : index + 1;
          if (query.source && state.omitFromSource.has(Number(message.uid))) continue;
          if (inRange(range, key, byUid ? maxUid : maxSeq)) yield message;
        }
      }

      async fetchOne(
        range: string,
        query: Record<string, unknown>,
        options?: Record<string, unknown>,
      ) {
        state.fetchOneCalls.push({ range, query, options });
        return state.fetchOneMessage;
      }
    },
  };
});

/** What PostgreSQL does with U+0000 anywhere in a text/jsonb value. */
function pgRejectsNul(rows: Row[]): unknown {
  const hasNul = (v: unknown): boolean =>
    typeof v === "string"
      ? v.includes("\u0000")
      : Array.isArray(v)
        ? v.some(hasNul)
        : v !== null && typeof v === "object" && !(v instanceof Date)
          ? Object.values(v).some(hasNul)
          : false;
  return rows.some((row) => Object.values(row).some(hasNul))
    ? pgError("22021", 'invalid byte sequence for encoding "UTF8": 0x00')
    : null;
}

function pgError(code: string, message: string) {
  return Object.assign(new Error(message), { code, severity: "ERROR" });
}

/** Shape of drizzle's DrizzleQueryError: params carry every bound value. */
function drizzleError(cause: unknown, rows: Row[]) {
  return Object.assign(
    new Error(
      `Failed query: insert into "incoming_emails" ("id") values (default)\nparams: ${JSON.stringify(rows)}`,
    ),
    {
      query: 'insert into "incoming_emails" ("id") values (default)',
      params: rows,
      cause,
    },
  );
}

vi.mock("@workspace/db", () => ({
  db: {
    insert: () => ({
      values: (rows: Row[]) => ({
        onConflictDoNothing: () => ({
          returning: async () => {
            state.insertAttempts += 1;
            const verdict = pgRejectsNul(rows) ?? state.rejectInsert?.(rows);
            if (verdict) throw drizzleError(verdict, rows);
            const fresh = rows.filter(
              (row) => !state.storedKeys.has(`${String(row.smtpConfigId)}:${String(row.imapUid)}`),
            );
            for (const row of fresh) {
              state.storedKeys.add(`${String(row.smtpConfigId)}:${String(row.imapUid)}`);
            }
            state.insertedRows.push(...fresh);
            return fresh.map((row, index) => ({
              id: `incoming-${String(row.imapUid)}-${index + 1}`,
              imapUid: row.imapUid,
              fromAddress: row.fromAddress,
              fromName: row.fromName,
              subject: row.subject,
              receivedAt: row.receivedAt,
              sourceTruncated: row.sourceTruncated,
              contentTruncated: row.contentTruncated,
            }));
          },
        }),
      }),
    }),
  },
}));

vi.mock("mailparser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("mailparser")>();
  return {
    ...actual,
    simpleParser: (source: Buffer, options: Record<string, unknown>) =>
      source.includes("UNPARSEABLE-MARKER")
        ? Promise.reject(new Error("Unexpected end of MIME structure"))
        : actual.simpleParser(source, options),
  };
});

vi.mock("./smtp-configs.service", () => ({
  updateImapState: serviceMocks.updateImapState,
}));
vi.mock("./webhooks.service", () => ({
  dispatchEvent: serviceMocks.dispatchEvent,
}));
vi.mock("./bounce-handler", () => ({
  processBounce: serviceMocks.processBounce,
}));

import {
  backfillMailbox,
  fetchAttachment,
  IncomingEmailContentTooLargeError,
  resetMissingUidStreaks,
  syncMailbox,
  testImapConnection,
} from "./imap.service";
import {
  INCOMING_AUTOMATION_HEADER_MAX_CHARS,
  INCOMING_HTML_MAX_CHARS,
  INCOMING_SOURCE_MAX_BYTES,
  INCOMING_TEXT_MAX_CHARS,
} from "./incoming-email-limits";

const config = {
  id: "88d218c0-a39a-419b-9b44-2688967f971a",
  organizationId: "org-1",
  name: "Mailbox",
  username: "sender@example.test",
  imapHost: "imap.example.test",
  imapPort: 993,
  imapSecure: true,
  imapUsername: "sender@example.test",
  imapPassword: "test-only",
  imapLastUid: null,
  imapUidValidity: null,
  imapFirstUid: null,
  imapBackfillComplete: false,
} as unknown as SmtpConfig;

/** A mailbox whose forward cursor is valid (UIDVALIDITY 42) at `lastUid`. */
function syncedConfig(lastUid: number): SmtpConfig {
  return {
    ...config,
    imapLastUid: lastUid,
    imapUidValidity: 42,
    imapFirstUid: 1,
  } as SmtpConfig;
}

function messageSource(
  body: string,
  contentType = "text/plain",
  extraHeaders: string[] = [],
): Buffer {
  return Buffer.from(
    [
      "From: Sender <sender@example.test>",
      "To: Receiver <receiver@example.test>",
      "Subject: Bounded message",
      "Message-ID: <bounded@example.test>",
      ...extraHeaders,
      `Content-Type: ${contentType}; charset=utf-8`,
      "",
      body,
    ].join("\r\n"),
  );
}

function pushMessages(uids: number[], body = (uid: number) => `Reply ${uid}.`) {
  for (const uid of uids) {
    const source = messageSource(body(uid), "text/plain", [
      `In-Reply-To: <orig-${uid}@example.test>`,
    ]);
    state.messages.push({
      uid,
      source,
      size: source.length,
      internalDate: new Date("2026-10-20T08:00:00.000Z"),
    });
  }
}

/** Every updateImapState patch that moved the forward cursor. */
function cursorWrites(): number[] {
  return serviceMocks.updateImapState.mock.calls
    .map(([, patch]) => (patch as { lastUid?: number }).lastUid)
    .filter((uid): uid is number => typeof uid === "number");
}

function healthWrites(): Array<string | null | undefined> {
  return serviceMocks.updateImapState.mock.calls
    .map(([, patch]) => patch as { lastSyncAt?: Date; lastSyncError?: string | null })
    .filter((patch) => patch.lastSyncAt instanceof Date)
    .map((patch) => patch.lastSyncError);
}

beforeEach(() => {
  state.clientOptions.length = 0;
  state.clients.length = 0;
  state.fetchCalls.length = 0;
  state.fetchOneCalls.length = 0;
  state.messages.length = 0;
  state.fetchOneMessage = null;
  state.insertedRows.length = 0;
  state.insertAttempts = 0;
  state.mailbox = { uidValidity: 42, exists: 1 };
  state.connectError = null;
  state.lateError = null;
  state.rejectInsert = null;
  state.storedKeys.clear();
  state.omitFromSource.clear();
  resetMissingUidStreaks();
  vi.clearAllMocks();
  serviceMocks.processBounce.mockResolvedValue(undefined);
});

describe("bounded IMAP ingestion", () => {
  it("keeps a normal forward-sync message intact and bounds the protocol fetch", async () => {
    const source = messageSource("A normal reply.");
    state.messages.push({
      uid: 7,
      source,
      size: source.length,
      internalDate: new Date("2026-08-04T12:00:00.000Z"),
    });

    await expect(syncMailbox(config)).resolves.toEqual({
      fetched: 1,
      error: undefined,
    });

    expect(state.clientOptions[0]).toMatchObject({
      maxLiteralSize: INCOMING_SOURCE_MAX_BYTES,
      maxLineLength: INCOMING_SOURCE_MAX_BYTES,
    });
    // Phase 1 lists UIDs only; phase 2 fetches the bounded sources.
    expect(state.fetchCalls[0]?.query).toEqual({ uid: true });
    expect(state.fetchCalls[1]?.query).toMatchObject({
      uid: true,
      size: true,
      envelope: true,
      source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
    });
    expect(state.insertedRows[0]).toMatchObject({
      text: "A normal reply.",
      autoSubmitted: null,
      precedence: null,
      xAutoResponseSuppress: null,
      automationHeadersComplete: true,
      sourceSizeBytes: source.length,
      sourceTruncated: false,
      contentTruncated: false,
    });
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledWith(
      "org-1",
      "incoming.received",
      expect.objectContaining({
        sourceTruncated: false,
        contentTruncated: false,
      }),
    );
  });

  it("case-folds and stores only the three allowlisted automation headers", async () => {
    const source = messageSource("Automated reply.", "text/plain", [
      "aUtO-sUbMiTtEd: AuTo-GeNeRaTeD",
      "pReCeDeNcE: BuLk",
      "X-aUtO-rEsPoNsE-sUpPrEsS: OOF, AutoReply",
      "X-Loop: must-not-be-stored",
    ]);
    state.messages.push({
      uid: 71,
      source,
      size: source.length,
      internalDate: new Date("2026-08-04T12:00:30.000Z"),
    });

    await expect(syncMailbox(config)).resolves.toMatchObject({ fetched: 1 });

    const row = state.insertedRows[0];
    expect(row).toMatchObject({
      autoSubmitted: "auto-generated",
      precedence: "bulk",
      xAutoResponseSuppress: "oof, autoreply",
      automationHeadersComplete: true,
      contentTruncated: false,
    });
    expect(row).not.toHaveProperty("headers");
    expect(row).not.toHaveProperty("headerLines");
    expect(row).not.toHaveProperty("xLoop");
  });

  it("bounds automation header values and marks the stored content truncated", async () => {
    const oversized = "A".repeat(INCOMING_AUTOMATION_HEADER_MAX_CHARS + 17);
    const source = messageSource("Oversized safety header.", "text/plain", [
      `Auto-Submitted: ${oversized}`,
      `Precedence: ${oversized}`,
      `X-Auto-Response-Suppress: ${oversized}`,
    ]);
    state.messages.push({
      uid: 72,
      source,
      size: source.length,
      internalDate: new Date("2026-08-04T12:00:45.000Z"),
    });

    await expect(syncMailbox(config)).resolves.toMatchObject({ fetched: 1 });

    const row = state.insertedRows[0];
    expect((row?.autoSubmitted as string).length).toBe(
      INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect((row?.precedence as string).length).toBe(
      INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect((row?.xAutoResponseSuppress as string).length).toBe(
      INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect(row).toMatchObject({
      automationHeadersComplete: true,
      contentTruncated: true,
    });
  });

  it("stores only bounded bodies and marks an oversized forward-sync source", async () => {
    const text = "x".repeat(INCOMING_TEXT_MAX_CHARS + 4_096);
    const source = messageSource(text);
    state.messages.push({
      uid: 8,
      source,
      size: INCOMING_SOURCE_MAX_BYTES + 10_000,
      internalDate: new Date("2026-08-04T12:01:00.000Z"),
    });

    await expect(syncMailbox(config)).resolves.toMatchObject({ fetched: 1 });

    const row = state.insertedRows[0];
    expect((row?.text as string).length).toBe(INCOMING_TEXT_MAX_CHARS);
    expect(row).toMatchObject({
      sourceSizeBytes: INCOMING_SOURCE_MAX_BYTES + 10_000,
      sourceTruncated: true,
      contentTruncated: true,
    });
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledWith(
      "org-1",
      "incoming.received",
      expect.objectContaining({
        sourceTruncated: true,
        contentTruncated: true,
      }),
    );
  });

  it("uses the same bounded source request and storage policy for backfill", async () => {
    const html = `<p>${"h".repeat(INCOMING_HTML_MAX_CHARS + 1_024)}</p>`;
    const source = messageSource(html, "text/html");
    state.messages.push({
      uid: 499,
      source,
      size: INCOMING_SOURCE_MAX_BYTES + 1,
      internalDate: new Date("2026-08-03T12:00:00.000Z"),
    });
    const backfillConfig = {
      ...config,
      imapLastUid: 500,
      imapUidValidity: 42,
      imapFirstUid: 500,
    };

    await expect(backfillMailbox(backfillConfig)).resolves.toMatchObject({
      fetched: 1,
    });

    expect(state.fetchCalls[0]).toMatchObject({
      range: "450:499",
      query: expect.objectContaining({
        size: true,
        source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
      }),
      options: { uid: true },
    });
    expect((state.insertedRows[0]?.html as string).length).toBe(
      INCOMING_HTML_MAX_CHARS,
    );
    expect(state.insertedRows[0]).toMatchObject({
      sourceTruncated: true,
      contentTruncated: true,
    });
    expect(serviceMocks.dispatchEvent).not.toHaveBeenCalled();
  });
});

describe("a problematic message never wedges the mailbox", () => {
  it("strips the NULs of a mis-declared UTF-16 HTML part (the 20. 10. 2026 Alza message) and stores it", async () => {
    // UTF-16LE bytes labelled utf-8: decodes to "<\0p\0>\0H\0i\0…" — exactly
    // what PostgreSQL refused with 22021 on every sync for three weeks.
    const utf16 = Buffer.from("<p>Hi there</p>", "utf16le").toString("base64");
    const source = Buffer.from(
      [
        "From: Shop <shop@example.test>",
        "To: Receiver <receiver@example.test>",
        "Subject: Newsletter",
        "Message-ID: <utf16@example.test>",
        "Content-Type: text/html; charset=utf-8",
        "Content-Transfer-Encoding: base64",
        "",
        utf16,
      ].join("\r\n"),
    );
    state.messages.push({
      uid: 24802,
      source,
      size: source.length,
      internalDate: new Date("2026-09-29T05:01:00.000Z"),
    });

    await expect(syncMailbox(syncedConfig(24801))).resolves.toEqual({
      fetched: 1,
      error: undefined,
    });

    const html = state.insertedRows[0]?.html as string;
    expect(html).not.toContain("\u0000");
    expect(html).toContain("<p>Hi there</p>");
    expect(cursorWrites()).toEqual([24802]);
    expect(healthWrites()).toEqual([null]);
  });

  it("isolates a row the database rejects: neighbours are stored whole, it is stored envelope-only, the cursor moves on", async () => {
    pushMessages([11, 12, 13]);
    // Simulated value-level rejection (class 22) of uid 12's full row only.
    state.rejectInsert = (rows) =>
      rows.some((r) => r.imapUid === 12 && r.text !== null)
        ? pgError("22P02", "invalid input syntax")
        : null;

    const notice =
      "Synced with problems: 1 message(s) stored without content (uid 12; SQLSTATE 22P02)";
    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 3,
      notice,
    });

    const byUid = new Map(state.insertedRows.map((r) => [r.imapUid, r]));
    expect(byUid.get(11)).toMatchObject({ text: "Reply 11.", contentTruncated: false });
    expect(byUid.get(13)).toMatchObject({ text: "Reply 13.", contentTruncated: false });
    expect(byUid.get(12)).toMatchObject({
      text: null,
      html: null,
      toAddresses: [],
      attachments: null,
      contentTruncated: true,
      // Threading survives, so reply matching downstream still works.
      inReplyTo: "<orig-12@example.test>",
      fromAddress: "sender@example.test",
    });
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledTimes(3);
    expect(cursorWrites()).toEqual([13]);
    // Not a failure (no backoff), but visible in imap_last_sync_error.
    expect(healthWrites()).toEqual([notice]);
  });

  it("skips a message the database refuses even envelope-only, and still advances past it", async () => {
    pushMessages([11, 12, 13]);
    state.rejectInsert = (rows) =>
      rows.some((r) => r.imapUid === 12)
        ? pgError("23514", "new row violates check constraint")
        : null;

    const notice =
      "Synced with problems: 1 message(s) not stored, refused by the database (uid 12; SQLSTATE 23514)";
    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 2,
      notice,
    });
    expect(state.insertedRows.map((r) => r.imapUid).sort()).toEqual([11, 13]);
    expect(cursorWrites()).toEqual([13]);
    expect(healthWrites()).toEqual([notice]);
  });

  it("stops without moving the cursor when the database refuses EVERY row of the batch, even envelope-only", async () => {
    pushMessages([11, 12, 13], () => "SECRET BODY");
    // E.g. a NOT NULL column the code does not know about after a bad migration.
    state.rejectInsert = () =>
      pgError("23502", 'null value in column "x" of relation "incoming_emails" violates not-null constraint');

    const result = await syncMailbox(syncedConfig(10));

    expect(result.fetched).toBe(0);
    expect(result.notice).toBeUndefined();
    expect(result.error).toMatch(
      /^Database refused all 3 message\(s\) of the batch even envelope-only \(uid 11-13\).*cursor not advanced: Database error 23502: null value in column/,
    );
    expect(result.error).not.toContain("SECRET");
    expect(state.insertedRows).toHaveLength(0);
    expect(cursorWrites()).toEqual([]);
    expect(healthWrites()).toEqual([result.error]);
    expect(serviceMocks.dispatchEvent).not.toHaveBeenCalled();
  });

  it("a lone message the database refuses waits for newer mail, then only it is skipped", async () => {
    pushMessages([12]);
    state.rejectInsert = (rows) =>
      rows.some((r) => r.imapUid === 12)
        ? pgError("23514", "new row violates check constraint")
        : null;

    // Alone in its batch, the refusal cannot be told from a database problem.
    const first = await syncMailbox(syncedConfig(10));
    expect(first.error).toMatch(/^Database refused all 1 message\(s\)/);
    expect(cursorWrites()).toEqual([]);

    pushMessages([13]);
    const second = await syncMailbox(syncedConfig(10));
    expect(second).toEqual({
      fetched: 1,
      notice:
        "Synced with problems: 1 message(s) not stored, refused by the database (uid 12; SQLSTATE 23514)",
    });
    expect(cursorWrites()).toEqual([13]);
  });

  it("aborts on a transient database error without moving the cursor or leaking content", async () => {
    pushMessages([11, 12], () => "SECRET BODY");
    state.rejectInsert = () => pgError("08006", "connection terminated unexpectedly");

    const result = await syncMailbox(syncedConfig(10));

    expect(result.fetched).toBe(0);
    expect(result.error).toBe(
      "Database error 08006: connection terminated unexpectedly (insert into incoming_emails)",
    );
    // One statement, no row-by-row retries for a non-data error.
    expect(state.insertAttempts).toBe(1);
    expect(cursorWrites()).toEqual([]);
    const [stored] = healthWrites();
    expect(stored).toBe(result.error);
    expect(stored).not.toContain("SECRET");
    expect(stored).not.toContain("params");
    expect(serviceMocks.dispatchEvent).not.toHaveBeenCalled();
  });

  it("downloads only the bounded window, not every message above the cursor", async () => {
    pushMessages(Array.from({ length: 120 }, (_, i) => 101 + i));

    await expect(syncMailbox(syncedConfig(100))).resolves.toMatchObject({
      fetched: 50,
    });

    expect(state.fetchCalls).toHaveLength(2);
    expect(state.fetchCalls[0]).toMatchObject({
      range: "101:*",
      query: { uid: true },
      options: { uid: true },
    });
    expect(state.fetchCalls[1]).toMatchObject({
      range: "101:150",
      options: { uid: true },
    });
    expect(state.insertedRows).toHaveLength(50);
    expect(cursorWrites()).toEqual([150]);
  });

  it("ignores the always-included highest UID when nothing is newer than the cursor", async () => {
    pushMessages([5]);

    await expect(syncMailbox(syncedConfig(9))).resolves.toEqual({
      fetched: 0,
      error: undefined,
    });
    expect(state.fetchCalls).toHaveLength(1);
    expect(state.insertedRows).toHaveLength(0);
    expect(cursorWrites()).toEqual([9]);
  });
});

describe("the forward cursor never silently passes a message", () => {
  it("holds the cursor before a listed UID the server did not return, and stores it once it comes back", async () => {
    pushMessages([11, 12, 13]);
    state.omitFromSource.add(12); // e.g. Gmail answering OK with fewer messages
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 2,
      error: undefined,
    });
    expect(cursorWrites()).toEqual([11]);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("did not return them (uid 12); cursor held at 11, attempt 1/3"),
    );

    state.omitFromSource.clear();
    await expect(syncMailbox(syncedConfig(11))).resolves.toEqual({
      fetched: 1,
      error: undefined,
    });
    expect(cursorWrites()).toEqual([11, 13]);
    expect(state.insertedRows.map((r) => r.imapUid)).toEqual([11, 13, 12]);
    // 13 was fetched twice but announced once (unique index).
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledTimes(3);
    warn.mockRestore();
  });

  it("passes a UID that is listed but never returned after 3 syncs, and says so", async () => {
    pushMessages([11, 12, 13]);
    state.omitFromSource.add(12);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    await syncMailbox(syncedConfig(10)); // attempt 1 → held at 11
    await syncMailbox(syncedConfig(11)); // attempt 2 → held at 11
    const third = await syncMailbox(syncedConfig(11)); // attempt 3 → passed

    expect(cursorWrites()).toEqual([11, 11, 13]);
    const notice =
      "Synced with problems: uid 12 never returned by the server in 3 syncs, passed (not stored)";
    expect(third).toEqual({ fetched: 0, notice });
    expect(healthWrites()).toEqual([null, null, notice]);
    expect(error).toHaveBeenCalledWith(
      expect.stringContaining("uid 12 was listed but never returned by the server in 3 syncs"),
    );
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledTimes(2);
    warn.mockRestore();
    error.mockRestore();
  });

  it("passes an unparseable message (final) but notes it", async () => {
    pushMessages([11, 12], (uid) => (uid === 12 ? "UNPARSEABLE-MARKER" : `Reply ${uid}.`));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 1,
      notice: "Synced with problems: 1 message(s) not stored, unparseable (uid 12)",
    });
    expect(cursorWrites()).toEqual([12]);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("uid 12 is not a parseable message; not stored"),
    );
    warn.mockRestore();
  });
});

describe("bounces", () => {
  function pushBounce(uid: number, recipient: string) {
    const source = Buffer.from(
      [
        "From: Mail Delivery System <MAILER-DAEMON@example.test>",
        "To: bounces+0f7b2d7e-5a43-5b8b-9f0c-3a2f6e0d9c11@example.test",
        "Subject: Undelivered Mail Returned to Sender",
        `Message-ID: <dsn-${uid}@example.test>`,
        "Content-Type: text/plain; charset=utf-8",
        "",
        `Final-Recipient: rfc822; ${recipient}`,
        "Action: failed",
        "Status: 5.1.1",
        "Diagnostic-Code: smtp; 550 5.1.1 user unknown",
      ].join("\r\n"),
    );
    state.messages.push({ uid, source, size: source.length });
  }

  it("one bounce the database refuses neither aborts the sync nor loses the batch's webhooks", async () => {
    pushMessages([11]);
    pushBounce(12, "dead@example.test");
    pushBounce(13, "gone@example.test");
    serviceMocks.processBounce.mockImplementation(async (_org: string, b: { recipient?: string }) => {
      if (b.recipient === "dead@example.test") {
        throw drizzleError(pgError("22P02", "invalid input syntax for type uuid"), []);
      }
    });
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 3,
      notice: "Synced with problems: 1 bounce(s) not processed (uid 12)",
    });
    expect(serviceMocks.processBounce).toHaveBeenCalledTimes(2);
    expect(serviceMocks.dispatchEvent).toHaveBeenCalledTimes(3);
    expect(cursorWrites()).toEqual([13]);
    expect(error).toHaveBeenCalledWith(
      expect.stringContaining("bounce in uid 12 could not be processed: Database error 22P02"),
    );
    error.mockRestore();
  });
});

describe("IMAP connection failures stay contained", () => {
  it("handles an 'error' event emitted after connect() already rejected, closes the socket and records a concise error", async () => {
    state.connectError = Object.assign(new Error("Socket timeout"), {
      code: "ETIMEOUT",
      _connId: "c1",
    });
    state.lateError = Object.assign(new Error("Connection not available"), {
      code: "NoConnection",
      _connId: "c1",
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    await expect(syncMailbox(syncedConfig(10))).resolves.toEqual({
      fetched: 0,
      error: "Socket timeout [ETIMEOUT]",
    });
    // Let the late 'error' event fire: without a listener it would throw and
    // crash the process (vitest reports it as an unhandled error).
    await new Promise((resolve) => setImmediate(resolve));

    const client = state.clients[0]!;
    expect(client.listenerCount("error")).toBeGreaterThan(0);
    expect(client.closed).toBe(true);
    expect(healthWrites()).toEqual(["Socket timeout [ETIMEOUT]"]);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("Connection not available"),
    );
    warn.mockRestore();
  });

  it("surfaces the server's own words for a failed login (wrong app password)", async () => {
    state.connectError = Object.assign(new Error("Command failed"), {
      responseStatus: "NO",
      responseText: "[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
      serverResponseCode: "AUTHENTICATIONFAILED",
      authenticationFailed: true,
    });

    await expect(
      testImapConnection({
        imapHost: "imap.example.test",
        imapPort: 993,
        imapSecure: true,
        imapUsername: "sender@example.test",
        imapPassword: "wrong",
        username: "sender@example.test",
      }),
    ).resolves.toEqual({
      success: false,
      error:
        "Command failed (AUTHENTICATIONFAILED) - server: [AUTHENTICATIONFAILED] Invalid credentials (Failure)",
    });
    expect(state.clients[0]!.listenerCount("error")).toBeGreaterThan(0);
    expect(state.clients[0]!.closed).toBe(true);
  });

  it("gives every client it creates an 'error' listener (sync, backfill, attachment, test)", async () => {
    pushMessages([11]);
    await syncMailbox(syncedConfig(10));
    await backfillMailbox({ ...syncedConfig(10), imapFirstUid: 5 } as SmtpConfig);
    state.fetchOneMessage = null;
    await fetchAttachment(config, 11, 0);
    await testImapConnection(config);

    expect(state.clients.length).toBeGreaterThanOrEqual(4);
    for (const client of state.clients) {
      expect(client.listenerCount("error")).toBeGreaterThan(0);
    }
  });
});

describe("bounded attachment retrieval", () => {
  it("refuses an attachment when the RFC822 source was only partially fetched", async () => {
    const source = messageSource("partial");
    state.fetchOneMessage = {
      uid: 9,
      source,
      size: INCOMING_SOURCE_MAX_BYTES + 1,
    };

    await expect(fetchAttachment(config, 9, 0)).rejects.toBeInstanceOf(
      IncomingEmailContentTooLargeError,
    );
    expect(state.fetchOneCalls[0]?.query).toEqual({
      size: true,
      source: { start: 0, maxLength: INCOMING_SOURCE_MAX_BYTES },
    });
  });
});
