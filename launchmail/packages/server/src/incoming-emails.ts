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
  getSmtpConfig,
  getSmtpConfigById,
  enqueueEmail,
  addSuppression,
  recordAudit,
  type InboxFolder,
} from "@workspace/mail-queue";
import type { AppVariables } from ".";
import { requirePerm } from "./org-context";

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
    return c.json(await listMailboxes(c.get("organizationId")!));
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
    return c.json(
      await listIncomingEmails(c.get("organizationId")!, {
        smtpConfigId: c.req.query("smtpConfigId") || undefined,
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
      const config = await getSmtpConfig(c.req.valid("json").smtpConfigId, orgId);
      if (!config) return c.json({ error: "Not found" }, 404);
      if (!config.imapHost) {
        return c.json(
          { error: "This connection has no incoming (IMAP) mailbox configured" },
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
      const config = await getSmtpConfig(c.req.valid("json").smtpConfigId, orgId);
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
    const msg = await getIncomingEmail(c.req.param("id"), c.get("organizationId")!);
    if (!msg) return c.json({ error: "Not found" }, 404);
    return c.json(msg);
  })

  // Mark read / unread.
  .post(
    "/:id/read",
    zValidator("json", z.object({ seen: z.boolean().optional() })),
    async (c) => {
      const denied = requirePerm(c, "inbox", "read");
      if (denied) return denied;
      const ok = await setIncomingEmailSeen(
        c.req.param("id"),
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
      const ok = await setIncomingEmailStarred(
        c.req.param("id"),
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
      const ok = await setIncomingEmailArchived(
        c.req.param("id"),
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
    const ok = await deleteIncomingEmail(
      c.req.param("id"),
      c.get("organizationId")!,
    );
    if (ok) audit(c, "inbox.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  })

  // Block sender: add to the suppression list (no future sends) and archive it.
  .post("/:id/block", async (c) => {
    const denied = requirePerm(c, "inbox", "reply");
    if (denied) return denied;
    const orgId = c.get("organizationId")!;
    const msg = await getIncomingEmail(c.req.param("id"), orgId);
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
        })
        .refine((d) => (d.text && d.text.trim()) || (d.html && d.html.trim()), {
          message: "Reply body is required",
        }),
    ),
    async (c) => {
      const denied = requirePerm(c, "inbox", "reply");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const msg = await getIncomingEmail(c.req.param("id"), orgId);
      if (!msg) return c.json({ error: "Not found" }, 404);

      const config = await getSmtpConfig(msg.smtpConfigId, orgId);
      if (!config) return c.json({ error: "Mailbox connection not found" }, 400);

      const body = c.req.valid("json");
      const from = config.fromName
        ? `${config.fromName} <${config.fromAddress}>`
        : config.fromAddress;
      const baseSubject = (msg.subject ?? "").replace(/^\s*(re:\s*)+/i, "").trim();
      const subject = baseSubject ? `Re: ${baseSubject}` : "Re:";
      const references =
        [msg.references, msg.messageId].filter(Boolean).join(" ").trim() ||
        undefined;

      await enqueueEmail({
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
      });

      await markIncomingEmailReplied(msg.id, orgId);
      audit(c, "inbox.reply", msg.fromAddress);
      return c.json({ success: true });
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
      const msg = await getIncomingEmail(c.req.param("id"), orgId);
      if (!msg) return c.json({ error: "Not found" }, 404);

      const config = await getSmtpConfig(msg.smtpConfigId, orgId);
      if (!config) return c.json({ error: "Mailbox connection not found" }, 400);

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
    const msg = await getIncomingEmail(c.req.param("id"), orgId);
    if (!msg) return c.json({ error: "Not found" }, 404);
    const config = await getSmtpConfigById(msg.smtpConfigId);
    if (!config || config.organizationId !== orgId) {
      return c.json({ error: "Mailbox not found" }, 404);
    }

    const index = Number(c.req.param("index"));
    if (!Number.isInteger(index) || index < 0) {
      return c.json({ error: "Invalid attachment index" }, 400);
    }
    const att = await fetchAttachment(config, msg.imapUid, index);
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
