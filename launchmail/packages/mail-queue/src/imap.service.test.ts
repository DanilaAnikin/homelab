import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SmtpConfig } from "./smtp-configs.service";

const state = vi.hoisted(() => ({
  clientOptions: [] as Array<Record<string, unknown>>,
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
  mailbox: { uidValidity: 42, exists: 1 },
}));

const serviceMocks = vi.hoisted(() => ({
  updateImapState: vi.fn(),
  dispatchEvent: vi.fn(),
  processBounce: vi.fn(),
}));

vi.mock("imapflow", () => ({
  ImapFlow: class {
    mailbox = state.mailbox;

    constructor(options: Record<string, unknown>) {
      state.clientOptions.push(options);
    }

    async connect() {}

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
      for (const message of state.messages) yield message;
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
}));

vi.mock("@workspace/db", () => ({
  db: {
    insert: () => ({
      values: (rows: Array<Record<string, unknown>>) => {
        state.insertedRows.push(...rows);
        return {
          onConflictDoNothing: () => ({
            returning: async () =>
              rows.map((row, index) => ({
                id: `incoming-${index + 1}`,
                imapUid: row.imapUid,
                fromAddress: row.fromAddress,
                fromName: row.fromName,
                subject: row.subject,
                receivedAt: row.receivedAt,
                sourceTruncated: row.sourceTruncated,
                contentTruncated: row.contentTruncated,
              })),
          }),
        };
      },
    }),
  },
}));

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
  syncMailbox,
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

beforeEach(() => {
  state.clientOptions.length = 0;
  state.fetchCalls.length = 0;
  state.fetchOneCalls.length = 0;
  state.messages.length = 0;
  state.fetchOneMessage = null;
  state.insertedRows.length = 0;
  state.mailbox = { uidValidity: 42, exists: 1 };
  vi.clearAllMocks();
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
    expect(state.fetchCalls[0]?.query).toMatchObject({
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
