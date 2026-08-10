import { Hono } from "hono";
import { zValidator } from "@hono/zod-validator";
import { z } from "zod";
import {
  listMailboxes,
  listIncomingEmails,
  getIncomingEmail,
  setIncomingEmailSeen,
  setIncomingEmailStarred,
  setIncomingEmailArchived,
  deleteIncomingEmail,
  markIncomingEmailReplied,
  syncMailbox,
  backfillMailbox,
  fetchAttachment,
  IncomingEmailContentTooLargeError,
  INCOMING_ADDRESS_MAX_CHARS,
  INCOMING_ADDRESS_MAX_ITEMS,
  INCOMING_ATTACHMENT_MAX_ITEMS,
  INCOMING_AUTOMATION_HEADER_MAX_CHARS,
  INCOMING_CONTENT_TYPE_MAX_CHARS,
  INCOMING_FILENAME_MAX_CHARS,
  INCOMING_HEADER_MAX_CHARS,
  INCOMING_HTML_MAX_CHARS,
  INCOMING_NAME_MAX_CHARS,
  INCOMING_SUBJECT_MAX_CHARS,
  INCOMING_TEXT_MAX_CHARS,
  getSmtpConfig,
  getSmtpConfigById,
  enqueueEmail,
  addSuppression,
  recordAudit,
  type InboxFolder,
} from "@workspace/mail-queue";
import type { AppVariables } from ".";
import { requirePerm } from "./org-context";

type InboxContext = Parameters<typeof requirePerm>[0];

function boundedString(
  value: unknown,
  max: number,
): { value: unknown; truncated: boolean } {
  if (typeof value !== "string" || value.length <= max) {
    return { value, truncated: false };
  }
  return { value: value.slice(0, max), truncated: true };
}

/**
 * Final response boundary for message detail. Ingestion and the DB projection
 * already apply these limits; keeping a serializer guard means an old row or a
 * future service regression still cannot produce an unbounded JSON response.
 */
function boundedIncomingEmailDetail(
  message: Record<string, unknown>,
): Record<string, unknown> {
  const detail = { ...message };
  let truncated = detail.contentTruncated === true;
  const stringLimits: Record<string, number> = {
    messageId: INCOMING_HEADER_MAX_CHARS,
    inReplyTo: INCOMING_HEADER_MAX_CHARS,
    references: INCOMING_HEADER_MAX_CHARS,
    autoSubmitted: INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    precedence: INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    xAutoResponseSuppress: INCOMING_AUTOMATION_HEADER_MAX_CHARS,
    fromAddress: INCOMING_ADDRESS_MAX_CHARS,
    fromName: INCOMING_NAME_MAX_CHARS,
    subject: INCOMING_SUBJECT_MAX_CHARS,
    snippet: 240,
    text: INCOMING_TEXT_MAX_CHARS,
    html: INCOMING_HTML_MAX_CHARS,
  };
  for (const [key, max] of Object.entries(stringLimits)) {
    const bounded = boundedString(detail[key], max);
    detail[key] = bounded.value;
    truncated ||= bounded.truncated;
  }

  for (const key of ["toAddresses", "ccAddresses"] as const) {
    if (!Array.isArray(detail[key])) continue;
    const source = detail[key] as unknown[];
    if (source.length > INCOMING_ADDRESS_MAX_ITEMS) truncated = true;
    detail[key] = source.slice(0, INCOMING_ADDRESS_MAX_ITEMS).map((entry) => {
      if (!entry || typeof entry !== "object") return {};
      const record = entry as Record<string, unknown>;
      const email = boundedString(record.email, INCOMING_ADDRESS_MAX_CHARS);
      const name = boundedString(record.name, INCOMING_NAME_MAX_CHARS);
      truncated ||= email.truncated || name.truncated;
      return {
        ...(typeof email.value === "string" ? { email: email.value } : {}),
        ...(typeof name.value === "string" ? { name: name.value } : {}),
      };
    });
  }

  if (Array.isArray(detail.attachments)) {
    const source = detail.attachments as unknown[];
    if (source.length > INCOMING_ATTACHMENT_MAX_ITEMS) truncated = true;
    detail.attachments = source
      .slice(0, INCOMING_ATTACHMENT_MAX_ITEMS)
      .map((entry) => {
        if (!entry || typeof entry !== "object") return {};
        const record = entry as Record<string, unknown>;
        const filename = boundedString(
          record.filename,
          INCOMING_FILENAME_MAX_CHARS,
        );
        const contentType = boundedString(
          record.contentType,
          INCOMING_CONTENT_TYPE_MAX_CHARS,
        );
        truncated ||= filename.truncated || contentType.truncated;
        return {
          ...(typeof filename.value === "string"
            ? { filename: filename.value }
            : {}),
          ...(typeof contentType.value === "string"
            ? { contentType: contentType.value }
            : {}),
          size:
            typeof record.size === "number" && Number.isFinite(record.size)
              ? Math.max(0, Math.min(Number.MAX_SAFE_INTEGER, record.size))
              : 0,
        };
      });
  }

  detail.contentTruncated = truncated;
  return detail;
}

async function authorizedIncomingEmail(c: InboxContext, id: string) {
  const msg = await getIncomingEmail(id, c.get("organizationId")!);
  const boundConfigId = c.get("apiTokenSmtpConfigId");
  if (!msg || (boundConfigId && msg.smtpConfigId !== boundConfigId)) {
    return null;
  }
  return msg;
}

function authorizedConfigId(
  c: InboxContext,
  requestedConfigId: string | undefined,
): string | null {
  const boundConfigId = c.get("apiTokenSmtpConfigId");
  if (
    boundConfigId &&
    requestedConfigId &&
    requestedConfigId !== boundConfigId
  ) {
    return null;
  }
  return boundConfigId ?? requestedConfigId ?? null;
}

function audit(
  c: Parameters<typeof requirePerm>[0],
  action: string,
  target?: string,
) {
  const user = c.get("user");
  void recordAudit({
    organizationId: c.get("organizationId")!,
    userId: user?.id,
    userName: user?.name,
    action,
    target,
  });
}

const recipients = z.array(
  z.object({ email: z.string().email(), name: z.string().optional() }),
);
// Attachments arrive base64-encoded. Cap count + size so a single send can't
// blow up worker memory / the Redis queue payload (worst case now ~35MB/send).
const attachments = z
  .array(
    z.object({
      filename: z.string().min(1),
      content: z.string().max(9_500_000), // ~7MB decoded
      contentType: z.string().optional(),
    }),
  )
  .max(5)
  .optional();

const FOLDERS = new Set<InboxFolder>(["inbox", "archived", "starred", "all"]);

const incomingEmailsRouter = new Hono<AppVariables>()
  // Receive-enabled connections (the picker) — each with unread + sync health.
  .get("/mailboxes", async (c) => {
    const denied = requirePerm(c, "inbox", "read");
    if (denied) return denied;
    const boundConfigId = c.get("apiTokenSmtpConfigId");
    const mailboxes = await listMailboxes(c.get("organizationId")!);
    return c.json(
      boundConfigId
        ? mailboxes.filter((mailbox) => mailbox.id === boundConfigId)
        : mailboxes,
    );
  })

  // Inbox list. ?smtpConfigId, ?folder (inbox|archived|starred|all), ?q search,
  // ?before=<iso> pagination.
  .get("/", async (c) => {
    const denied = requirePerm(c, "inbox", "read");
    if (denied) return denied;
    const folderRaw = c.req.query("folder") as InboxFolder | undefined;
    const folder = folderRaw && FOLDERS.has(folderRaw) ? folderRaw : "inbox";
    const limit = Number(c.req.query("limit") ?? 50);
    const beforeRaw = c.req.query("before");
    const before = beforeRaw ? new Date(beforeRaw) : undefined;
    const smtpConfigId = authorizedConfigId(
      c,
      c.req.query("smtpConfigId") || undefined,
    );
    if (!smtpConfigId && c.get("apiTokenSmtpConfigId")) {
      return c.json({ error: "Not found" }, 404);
    }
    return c.json(
      await listIncomingEmails(c.get("organizationId")!, {
        smtpConfigId: smtpConfigId ?? undefined,
        folder,
        q: c.req.query("q") || undefined,
        limit: Number.isFinite(limit) ? limit : 50,
        before: before && !Number.isNaN(before.getTime()) ? before : undefined,
      }),
    );
  })

  // Manual "Sync now" for one mailbox.
  .post(
    "/sync",
    zValidator("json", z.object({ smtpConfigId: z.string().uuid() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "sync");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const smtpConfigId = authorizedConfigId(
        c,
        c.req.valid("json").smtpConfigId,
      );
      if (!smtpConfigId) return c.json({ error: "Not found" }, 404);
      const config = await getSmtpConfig(smtpConfigId, orgId);
      if (!config) return c.json({ error: "Not found" }, 404);
      if (!config.imapHost) {
        return c.json(
          {
            error: "This connection has no incoming (IMAP) mailbox configured",
          },
          400,
        );
      }
      const result = await syncMailbox(config);
      if (result.error) {
        return c.json({ error: result.error, fetched: result.fetched }, 502);
      }
      return c.json({ fetched: result.fetched });
    },
  )

  // Pull one batch of older history (progressive backfill).
  .post(
    "/backfill",
    zValidator("json", z.object({ smtpConfigId: z.string().uuid() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "sync");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const smtpConfigId = authorizedConfigId(
        c,
        c.req.valid("json").smtpConfigId,
      );
      if (!smtpConfigId) return c.json({ error: "Not found" }, 404);
      const config = await getSmtpConfig(smtpConfigId, orgId);
      if (!config) return c.json({ error: "Not found" }, 404);
      if (!config.imapHost) {
        return c.json({ error: "No IMAP mailbox configured" }, 400);
      }
      const result = await backfillMailbox(config);
      if (result.error) {
        return c.json({ error: result.error, fetched: result.fetched }, 502);
      }
      return c.json({
        fetched: result.fetched,
        complete: result.fetched === 0,
      });
    },
  )

  // Full message (body + attachments metadata).
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "inbox", "read");
    if (denied) return denied;
    const msg = await authorizedIncomingEmail(c, c.req.param("id"));
    if (!msg) return c.json({ error: "Not found" }, 404);
    return c.json(
      boundedIncomingEmailDetail(msg as unknown as Record<string, unknown>),
    );
  })

  // Mark read / unread.
  .post(
    "/:id/read",
    zValidator("json", z.object({ seen: z.boolean().optional() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "read");
      if (denied) return denied;
      const msg = await authorizedIncomingEmail(c, c.req.param("id"));
      if (!msg) return c.json({ error: "Not found" }, 404);
      const ok = await setIncomingEmailSeen(
        msg.id,
        c.get("organizationId")!,
        c.req.valid("json").seen ?? true,
      );
      return c.json({ success: ok }, ok ? 200 : 404);
    },
  )

  // Star / unstar.
  .post(
    "/:id/star",
    zValidator("json", z.object({ starred: z.boolean() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "read");
      if (denied) return denied;
      const msg = await authorizedIncomingEmail(c, c.req.param("id"));
      if (!msg) return c.json({ error: "Not found" }, 404);
      const ok = await setIncomingEmailStarred(
        msg.id,
        c.get("organizationId")!,
        c.req.valid("json").starred,
      );
      return c.json({ success: ok }, ok ? 200 : 404);
    },
  )

  // Archive / unarchive.
  .post(
    "/:id/archive",
    zValidator("json", z.object({ archived: z.boolean() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "read");
      if (denied) return denied;
      const msg = await authorizedIncomingEmail(c, c.req.param("id"));
      if (!msg) return c.json({ error: "Not found" }, 404);
      const ok = await setIncomingEmailArchived(
        msg.id,
        c.get("organizationId")!,
        c.req.valid("json").archived,
      );
      return c.json({ success: ok }, ok ? 200 : 404);
    },
  )

  // Permanently delete from our store.
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "inbox", "reply");
    if (denied) return denied;
    const msg = await authorizedIncomingEmail(c, c.req.param("id"));
    if (!msg) return c.json({ error: "Not found" }, 404);
    const ok = await deleteIncomingEmail(msg.id, c.get("organizationId")!);
    if (ok) audit(c, "inbox.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  })

  // Block sender: add to the suppression list (no future sends) and archive it.
  .post("/:id/block", async (c) => {
    const denied = requirePerm(c, "inbox", "reply");
    if (denied) return denied;
    const orgId = c.get("organizationId")!;
    const msg = await authorizedIncomingEmail(c, c.req.param("id"));
    if (!msg) return c.json({ error: "Not found" }, 404);
    await addSuppression(orgId, msg.fromAddress, "manual");
    await setIncomingEmailArchived(msg.id, orgId, true);
    audit(c, "inbox.block", msg.fromAddress);
    return c.json({ success: true });
  })

  // Reply: send via the same connection's SMTP side, threaded, logged, and stamp
  // repliedAt. Body (html/text) is built client-side (incl. the quoted original).
  .post(
    "/:id/reply",
    zValidator(
      "json",
      z
        .object({
          text: z.string().optional(),
          html: z.string().optional(),
          cc: recipients.optional(),
          bcc: recipients.optional(),
          attachments,
          clientReference: z.string().uuid().optional(),
          // Inbox replies are privileged automation paths. Keep routing
          // namespaces explicit instead of accepting arbitrary caller labels.
          clientType: z.enum(["freio_inbox_reply"]).optional(),
        })
        .refine((d) => (d.text && d.text.trim()) || (d.html && d.html.trim()), {
          message: "Reply body is required",
        }),
    ),
    async (c) => {
      const denied = requirePerm(c, "inbox", "reply");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const msg = await authorizedIncomingEmail(c, c.req.param("id"));
      if (!msg) return c.json({ error: "Not found" }, 404);

      const config = await getSmtpConfig(msg.smtpConfigId, orgId);
      if (!config)
        return c.json({ error: "Mailbox connection not found" }, 400);

      const body = c.req.valid("json");
      const from = config.fromName
        ? `${config.fromName} <${config.fromAddress}>`
        : config.fromAddress;
      const baseSubject = (msg.subject ?? "")
        .replace(/^\s*(re:\s*)+/i, "")
        .trim();
      const subject = baseSubject ? `Re: ${baseSubject}` : "Re:";
      const references =
        [msg.references, msg.messageId].filter(Boolean).join(" ").trim() ||
        undefined;

      const job = await enqueueEmail({
        smtpConfigId: config.id,
        organizationId: orgId,
        userId: c.get("user")?.id ?? null,
        from,
        to: [
          msg.fromName
            ? { email: msg.fromAddress, name: msg.fromName }
            : { email: msg.fromAddress },
        ],
        cc: body.cc,
        bcc: body.bcc,
        subject,
        html: body.html,
        text: body.text,
        inReplyTo: msg.messageId ?? undefined,
        references,
        attachments: body.attachments,
        clientReference: body.clientReference,
        clientType: body.clientType,
      });

      await markIncomingEmailReplied(msg.id, orgId);
      audit(c, "inbox.reply", msg.fromAddress);
      return c.json({
        success: true,
        id: String(job.id),
        status: "queued",
        smtpConfigId: config.id,
        clientReference: body.clientReference ?? null,
        clientType: body.clientType ?? null,
      });
    },
  )

  // Forward to new recipients (new conversation — not threaded onto the original).
  .post(
    "/:id/forward",
    zValidator(
      "json",
      z.object({
        to: recipients.min(1),
        cc: recipients.optional(),
        bcc: recipients.optional(),
        text: z.string().optional(),
        html: z.string().optional(),
        attachments,
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "inbox", "reply");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const msg = await authorizedIncomingEmail(c, c.req.param("id"));
      if (!msg) return c.json({ error: "Not found" }, 404);

      const config = await getSmtpConfig(msg.smtpConfigId, orgId);
      if (!config)
        return c.json({ error: "Mailbox connection not found" }, 400);

      const body = c.req.valid("json");
      const from = config.fromName
        ? `${config.fromName} <${config.fromAddress}>`
        : config.fromAddress;
      const base = (msg.subject ?? "").replace(/^\s*(fwd:\s*)+/i, "").trim();
      const subject = base ? `Fwd: ${base}` : "Fwd:";

      await enqueueEmail({
        smtpConfigId: config.id,
        organizationId: orgId,
        userId: c.get("user")?.id ?? null,
        from,
        to: body.to,
        cc: body.cc,
        bcc: body.bcc,
        subject,
        html: body.html,
        text: body.text,
        attachments: body.attachments,
      });

      audit(c, "inbox.forward", body.to.map((r) => r.email).join(", "));
      return c.json({ success: true });
    },
  )

  // On-demand attachment download (we store metadata, not blobs).
  .get("/:id/attachments/:index", async (c) => {
    const denied = requirePerm(c, "inbox", "read");
    if (denied) return denied;
    const orgId = c.get("organizationId")!;
    const msg = await authorizedIncomingEmail(c, c.req.param("id"));
    if (!msg) return c.json({ error: "Not found" }, 404);
    const config = await getSmtpConfigById(msg.smtpConfigId);
    if (!config || config.organizationId !== orgId) {
      return c.json({ error: "Mailbox not found" }, 404);
    }

    const index = Number(c.req.param("index"));
    if (!Number.isInteger(index) || index < 0) {
      return c.json({ error: "Invalid attachment index" }, 400);
    }
    let att;
    try {
      att = await fetchAttachment(config, msg.imapUid, index);
    } catch (error) {
      if (error instanceof IncomingEmailContentTooLargeError) {
        return c.json(
          { error: "Message exceeds the safe attachment download limit" },
          413,
        );
      }
      throw error;
    }
    if (!att) return c.json({ error: "Attachment not found" }, 404);

    const safeName =
      att.filename.replace(/["\r\n]/g, "").slice(0, 200) || "attachment";
    c.header("Content-Type", att.contentType || "application/octet-stream");
    c.header("Content-Disposition", `attachment; filename="${safeName}"`);
    const ab = att.content.buffer.slice(
      att.content.byteOffset,
      att.content.byteOffset + att.content.byteLength,
    ) as ArrayBuffer;
    return c.body(ab);
  });

export default incomingEmailsRouter;
