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
