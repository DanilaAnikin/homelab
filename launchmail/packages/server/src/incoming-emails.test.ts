import { Hono } from "hono";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppVariables } from ".";

const queueMocks = vi.hoisted(() => ({
  listMailboxes: vi.fn(),
  listIncomingEmails: vi.fn(),
  getIncomingEmail: vi.fn(),
  setIncomingEmailSeen: vi.fn(),
  setIncomingEmailStarred: vi.fn(),
  setIncomingEmailArchived: vi.fn(),
  deleteIncomingEmail: vi.fn(),
  markIncomingEmailReplied: vi.fn(),
  syncMailbox: vi.fn(),
  backfillMailbox: vi.fn(),
  fetchAttachment: vi.fn(),
  IncomingEmailContentTooLargeError: class extends Error {},
  INCOMING_ADDRESS_MAX_CHARS: 320,
  INCOMING_ADDRESS_MAX_ITEMS: 100,
  INCOMING_ATTACHMENT_MAX_ITEMS: 100,
  INCOMING_AUTOMATION_HEADER_MAX_CHARS: 512,
  INCOMING_CONTENT_TYPE_MAX_CHARS: 255,
  INCOMING_FILENAME_MAX_CHARS: 255,
  INCOMING_HEADER_MAX_CHARS: 8 * 1024,
  INCOMING_HTML_MAX_CHARS: 512 * 1024,
  INCOMING_NAME_MAX_CHARS: 512,
  INCOMING_SUBJECT_MAX_CHARS: 2 * 1024,
  INCOMING_TEXT_MAX_CHARS: 256 * 1024,
  getSmtpConfig: vi.fn(),
  getSmtpConfigById: vi.fn(),
  enqueueEmail: vi.fn(),
  addSuppression: vi.fn(),
  recordAudit: vi.fn(),
}));

vi.mock("@workspace/mail-queue", () => queueMocks);
vi.mock("./org-context", () => ({ requirePerm: () => null }));

import incomingEmailsRouter from "./incoming-emails";

const ORGANIZATION_ID = "60ef206b-f766-4eb8-8ea0-ac3ca1e05cf9";
const BOUND_CONFIG_ID = "88d218c0-a39a-419b-9b44-2688967f971a";
const FOREIGN_CONFIG_ID = "954ac519-8d37-430e-a570-becb9f64a44d";

function inboxApp(boundConfigId: string | null = BOUND_CONFIG_ID) {
  return new Hono<AppVariables>()
    .basePath("/api")
    .use("*", async (c, next) => {
      c.set("organizationId", ORGANIZATION_ID);
      c.set("role", "writer");
      c.set("user", null);
      c.set("session", null);
      c.set("apiTokenName", "Freio");
      c.set("apiTokenSmtpConfigId", boundConfigId);
      await next();
    })
    .route("/incoming-emails", incomingEmailsRouter);
}

beforeEach(() => {
  vi.clearAllMocks();
  queueMocks.listIncomingEmails.mockResolvedValue([]);
  queueMocks.listMailboxes.mockResolvedValue([]);
  queueMocks.setIncomingEmailSeen.mockResolvedValue(true);
});

describe("SMTP-config-bound inbox listing", () => {
  it("forces the bound smtpConfigId when no filter is requested", async () => {
    const response = await inboxApp().request("/api/incoming-emails");

    expect(response.status).toBe(200);
    expect(queueMocks.listIncomingEmails).toHaveBeenCalledWith(
      ORGANIZATION_ID,
      expect.objectContaining({ smtpConfigId: BOUND_CONFIG_ID }),
    );
  });

  it("returns 404 and does not query when another smtpConfigId is requested", async () => {
    const response = await inboxApp().request(
      `/api/incoming-emails?smtpConfigId=${FOREIGN_CONFIG_ID}`,
    );

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Not found" });
    expect(queueMocks.listIncomingEmails).not.toHaveBeenCalled();
  });

  it("only exposes the bound mailbox in the mailbox picker", async () => {
    queueMocks.listMailboxes.mockResolvedValue([
      { id: BOUND_CONFIG_ID, name: "Freio" },
      { id: FOREIGN_CONFIG_ID, name: "Other project" },
    ]);

    const response = await inboxApp().request("/api/incoming-emails/mailboxes");

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual([
      { id: BOUND_CONFIG_ID, name: "Freio" },
    ]);
  });
});

describe("SMTP-config-bound message access", () => {
  it("returns 404 for a message belonging to another SMTP config", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "foreign-message",
      smtpConfigId: FOREIGN_CONFIG_ID,
      subject: "Private message",
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/foreign-message",
    );

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Not found" });
  });

  it("blocks mutations for a foreign message before any write occurs", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "foreign-message",
      smtpConfigId: FOREIGN_CONFIG_ID,
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/foreign-message/read",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ seen: true }),
      },
    );

    expect(response.status).toBe(404);
    expect(queueMocks.setIncomingEmailSeen).not.toHaveBeenCalled();
  });

  it("allows a message from the token's bound SMTP config", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      subject: "Reply",
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message",
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
    });
  });

  it("keeps a normal detail body intact and reports no truncation", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      subject: "Reply",
      text: "A normal reply.",
      html: "<p>A normal reply.</p>",
      contentTruncated: false,
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message",
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      text: "A normal reply.",
      html: "<p>A normal reply.</p>",
      contentTruncated: false,
    });
  });

  it("returns the bounded allowlisted automation headers in canonical detail", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      autoSubmitted: "auto-generated",
      precedence: "bulk",
      xAutoResponseSuppress: "oof, autoreply",
      contentTruncated: false,
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message",
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      autoSubmitted: "auto-generated",
      precedence: "bulk",
      xAutoResponseSuppress: "oof, autoreply",
      contentTruncated: false,
    });
  });

  it("bounds every large detail collection/body before JSON serialization", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      subject: "s".repeat(queueMocks.INCOMING_SUBJECT_MAX_CHARS + 1),
      autoSubmitted: "a".repeat(
        queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS + 1,
      ),
      precedence: "p".repeat(
        queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS + 1,
      ),
      xAutoResponseSuppress: "x".repeat(
        queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS + 1,
      ),
      text: "t".repeat(queueMocks.INCOMING_TEXT_MAX_CHARS + 1),
      html: "h".repeat(queueMocks.INCOMING_HTML_MAX_CHARS + 1),
      toAddresses: Array.from(
        { length: queueMocks.INCOMING_ADDRESS_MAX_ITEMS + 1 },
        () => ({
          email: `${"e".repeat(queueMocks.INCOMING_ADDRESS_MAX_CHARS)}@x`,
          name: "n".repeat(queueMocks.INCOMING_NAME_MAX_CHARS + 1),
        }),
      ),
      attachments: Array.from(
        { length: queueMocks.INCOMING_ATTACHMENT_MAX_ITEMS + 1 },
        () => ({
          filename: "f".repeat(queueMocks.INCOMING_FILENAME_MAX_CHARS + 1),
          contentType: "c".repeat(
            queueMocks.INCOMING_CONTENT_TYPE_MAX_CHARS + 1,
          ),
          size: 12,
        }),
      ),
      contentTruncated: false,
    });

    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message",
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(200);
    expect((body.subject as string).length).toBe(
      queueMocks.INCOMING_SUBJECT_MAX_CHARS,
    );
    expect((body.autoSubmitted as string).length).toBe(
      queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect((body.precedence as string).length).toBe(
      queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect((body.xAutoResponseSuppress as string).length).toBe(
      queueMocks.INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    );
    expect((body.text as string).length).toBe(
      queueMocks.INCOMING_TEXT_MAX_CHARS,
    );
    expect((body.html as string).length).toBe(
      queueMocks.INCOMING_HTML_MAX_CHARS,
    );
    expect(body.toAddresses).toHaveLength(
      queueMocks.INCOMING_ADDRESS_MAX_ITEMS,
    );
    expect(body.attachments).toHaveLength(
      queueMocks.INCOMING_ATTACHMENT_MAX_ITEMS,
    );
    expect(body.contentTruncated).toBe(true);
  });
});

describe("bounded attachment endpoint", () => {
  it("returns a bounded 413 response when source retrieval is oversized", async () => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      imapUid: 91,
    });
    queueMocks.getSmtpConfigById.mockResolvedValue({
      id: BOUND_CONFIG_ID,
      organizationId: ORGANIZATION_ID,
    });
    queueMocks.fetchAttachment.mockRejectedValue(
      new queueMocks.IncomingEmailContentTooLargeError(),
    );

    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message/attachments/0",
    );

    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({
      error: "Message exceeds the safe attachment download limit",
    });
  });
});

describe("inbox reply terminal correlation", () => {
  const clientReference = "e8b4fe0c-28fe-493a-962a-9886b53f9eed";

  beforeEach(() => {
    queueMocks.getIncomingEmail.mockResolvedValue({
      id: "freio-message",
      smtpConfigId: BOUND_CONFIG_ID,
      fromAddress: "school@example.cz",
      fromName: "School",
      subject: "Re: Freio",
      messageId: "<message@example.cz>",
      references: null,
    });
    queueMocks.getSmtpConfig.mockResolvedValue({
      id: BOUND_CONFIG_ID,
      organizationId: ORGANIZATION_ID,
      fromAddress: "contact@freio.cz",
      fromName: "Freio Contact",
    });
    queueMocks.enqueueEmail.mockResolvedValue({ id: "reply-job-1" });
    queueMocks.markIncomingEmailReplied.mockResolvedValue(true);
  });

  it("propagates an allowlisted client type and UUID into the queued job", async () => {
    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message/reply",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          text: "Děkujeme za odpověď.",
          clientReference,
          clientType: "freio_inbox_reply",
        }),
      },
    );

    expect(response.status).toBe(200);
    expect(queueMocks.enqueueEmail).toHaveBeenCalledWith(
      expect.objectContaining({
        clientReference,
        clientType: "freio_inbox_reply",
      }),
    );
    expect(await response.json()).toMatchObject({
      id: "reply-job-1",
      clientReference,
      clientType: "freio_inbox_reply",
    });
  });

  it("rejects unknown routing namespaces before enqueue", async () => {
    const response = await inboxApp().request(
      "/api/incoming-emails/freio-message/reply",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          text: "Děkujeme.",
          clientReference,
          clientType: "other_project",
        }),
      },
    );

    expect(response.status).toBe(400);
    expect(queueMocks.enqueueEmail).not.toHaveBeenCalled();
  });
});
