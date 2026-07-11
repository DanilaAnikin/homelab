import { Hono } from "hono";
import { zValidator } from "@hono/zod-validator";
import { z } from "zod";
import {
  listForms,
  getForm,
  createForm,
  updateForm,
  deleteForm,
  listSubmissions,
  listApiTokens,
  createApiToken,
  deleteApiToken,
  listEmailLogs,
  listTemplates,
  getTemplate,
  createTemplate,
  updateTemplate,
  deleteTemplate,
  listSuppressions,
  addSuppression,
  removeSuppression,
  listDomains,
  getDomain,
  createDomain,
  deleteDomain,
  verifyDomain,
  buildRecords,
  listWebhooks,
  createWebhook,
  updateWebhook,
  deleteWebhook,
  pingWebhook,
  getAnalytics,
  mailQueue,
  createAudience,
  listAudiences,
  getAudience,
  deleteAudience,
  addContacts,
  listContacts,
  getSubscribedContacts,
  removeContact,
  createBroadcast,
  completeBroadcast,
  listBroadcasts,
  getSmtpConfigById,
  getDefaultSmtpConfig,
  enqueueEmail,
  filterSuppressed,
  recordAudit,
  listAudit,
  signId,
} from "@workspace/mail-queue";
import { canGrantRole } from "@workspace/auth/permissions";
import { renderBlocks, renderCustomHtml } from "@workspace/templates";
import type { Context } from "hono";

function buildAuditEntry(c: Context<AppVariables>, action: string, target?: string) {
  const user = c.get("user");
  return {
    organizationId: c.get("organizationId")!,
    userId: user?.id,
    userName: user?.name,
    action,
    target,
  };
}

function audit(c: Context<AppVariables>, action: string, target?: string) {
  void recordAudit(buildAuditEntry(c, action, target));
}

// recordAudit swallows its own DB errors, so awaiting it for security-critical
// events cannot throw or interrupt the request; it just guarantees the entry is
// persisted before we respond.
async function auditAwait(c: Context<AppVariables>, action: string, target?: string) {
  await recordAudit(buildAuditEntry(c, action, target));
}

const PUBLIC_BASE = (
  process.env.PUBLIC_API_URL ??
  process.env.API_URL ??
  "http://localhost:5000"
).replace(/\/$/, "");
import type { AppVariables } from ".";
import { requirePerm } from "./org-context";

const brandingSchema = z.object({
  brandName: z.string().optional(),
  accent: z.string().optional(),
  footer: z.string().optional(),
  theme: z.enum(["light", "dark"]).optional(),
});

const settingsSchema = z.object({
  honeypot: z.boolean().optional(),
  redirectUrl: z.string().optional(),
  allowedOrigins: z.array(z.string()).optional(),
  storeSubmissions: z.boolean().optional(),
  maxPerHour: z.number().optional(),
  autoRespond: z.boolean().optional(),
  autoRespondSubject: z.string().optional(),
  autoRespondMessage: z.string().optional(),
});

export const formsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "form", "read");
    if (denied) return denied;
    return c.json(await listForms(c.get("organizationId")!));
  })
  .post(
    "/",
    zValidator(
      "json",
      z.object({
        name: z.string().min(1),
        templateKey: z.string().min(1),
        templateId: z.string().uuid().nullable().optional(),
        recipients: z.array(z.string().email()).min(1),
        subject: z.string().optional(),
        smtpConfigId: z.string().uuid().optional(),
        replyToField: z.string().optional(),
        branding: brandingSchema.optional(),
        settings: settingsSchema.optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "form", "create");
      if (denied) return denied;
      const data = c.req.valid("json");
      const form = await createForm(c.get("organizationId")!, data);
      audit(c, "form.create", form.name);
      return c.json(form, 201);
    },
  )
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "form", "read");
    if (denied) return denied;
    const form = await getForm(c.req.param("id"), c.get("organizationId")!);
    if (!form) return c.json({ error: "Not found" }, 404);
    return c.json(form);
  })
  .patch(
    "/:id",
    zValidator(
      "json",
      z.object({
        name: z.string().optional(),
        templateKey: z.string().optional(),
        templateId: z.string().uuid().nullable().optional(),
        recipients: z.array(z.string().email()).min(1).optional(),
        subject: z.string().nullable().optional(),
        enabled: z.boolean().optional(),
        smtpConfigId: z.string().uuid().nullable().optional(),
        replyToField: z.string().nullable().optional(),
        customHtml: z.string().nullable().optional(),
        branding: brandingSchema.optional(),
        settings: settingsSchema.optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "form", "update");
      if (denied) return denied;
      const form = await updateForm(
        c.req.param("id"),
        c.get("organizationId")!,
        c.req.valid("json"),
      );
      if (!form) return c.json({ error: "Not found" }, 404);
      audit(c, "form.update", form.name);
      return c.json(form);
    },
  )
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "form", "delete");
    if (denied) return denied;
    const ok = await deleteForm(c.req.param("id"), c.get("organizationId")!);
    if (ok) audit(c, "form.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  })
  .get("/:id/submissions", async (c) => {
    const denied = requirePerm(c, "submission", "read");
    if (denied) return denied;
    const form = await getForm(c.req.param("id"), c.get("organizationId")!);
    if (!form) return c.json({ error: "Not found" }, 404);
    return c.json(await listSubmissions(form.id));
  });

const blockSchema = z
  .object({
    id: z.string(),
    type: z.enum(["heading", "text", "button", "image", "divider", "spacer"]),
    text: z.string().optional(),
    url: z.string().optional(),
    alt: z.string().optional(),
    size: z.number().optional(),
  })
  .passthrough();

const templateBody = z.object({
  name: z.string().min(1).optional(),
  mode: z.enum(["html", "blocks"]).optional(),
  subject: z.string().nullable().optional(),
  html: z.string().nullable().optional(),
  blocks: z.array(blockSchema).nullable().optional(),
  accent: z.string().nullable().optional(),
  theme: z.enum(["light", "dark"]).nullable().optional(),
});

export const templatesRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "template", "read");
    if (denied) return denied;
    return c.json(await listTemplates(c.get("organizationId")!));
  })
  .post("/", zValidator("json", templateBody), async (c) => {
    const denied = requirePerm(c, "template", "create");
    if (denied) return denied;
    const data = c.req.valid("json");
    const tpl = await createTemplate(c.get("organizationId")!, {
      name: data.name ?? "Untitled template",
      mode: data.mode ?? "blocks",
      subject: data.subject,
      html: data.html,
      blocks: data.blocks as never,
      accent: data.accent,
      theme: data.theme,
    });
    audit(c, "template.create", tpl.name);
    return c.json(tpl, 201);
  })
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "template", "read");
    if (denied) return denied;
    const tpl = await getTemplate(c.req.param("id"), c.get("organizationId")!);
    if (!tpl) return c.json({ error: "Not found" }, 404);
    return c.json(tpl);
  })
  .patch("/:id", zValidator("json", templateBody), async (c) => {
    const denied = requirePerm(c, "template", "update");
    if (denied) return denied;
    const tpl = await updateTemplate(
      c.req.param("id"),
      c.get("organizationId")!,
      c.req.valid("json") as never,
    );
    if (!tpl) return c.json({ error: "Not found" }, 404);
    audit(c, "template.update", tpl.name);
    return c.json(tpl);
  })
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "template", "delete");
    if (denied) return denied;
    const ok = await deleteTemplate(
      c.req.param("id"),
      c.get("organizationId")!,
    );
    if (ok) audit(c, "template.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  });

export const apiKeysRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "apiKey", "read");
    if (denied) return denied;
    const keys = await listApiTokens(c.get("organizationId")!);
    return c.json(keys.map(({ tokenHash, ...rest }) => rest));
  })
  .post(
    "/",
    zValidator(
      "json",
      z.object({
        name: z.string().min(1),
        role: z.enum(["admin", "writer", "reader"]).optional(),
        smtpConfigId: z.string().uuid().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "apiKey", "create");
      if (denied) return denied;
      const data = c.req.valid("json");
      // Clamp the granted role to the caller's: a writer must never be able to
      // mint an admin key. Default to the caller's own role when unspecified so
      // we never escalate above the caller.
      const callerRole = c.get("role");
      const requestedRole = data.role ?? callerRole ?? "reader";
      if (!callerRole || !canGrantRole(callerRole, requestedRole)) {
        return c.json(
          { error: "You cannot grant a role higher than your own" },
          403,
        );
      }
      const key = await createApiToken(c.get("organizationId")!, {
        ...data,
        role: requestedRole,
      });
      const { tokenHash, ...rest } = key;
      await auditAwait(c, "apiKey.create", `${key.name} (${requestedRole})`);
      return c.json(rest, 201);
    },
  )
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "apiKey", "revoke");
    if (denied) return denied;
    const ok = await deleteApiToken(c.req.param("id"), c.get("organizationId")!);
    if (ok) await auditAwait(c, "apiKey.revoke", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  });

export const suppressionsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "suppression", "read");
    if (denied) return denied;
    return c.json(await listSuppressions(c.get("organizationId")!));
  })
  .post(
    "/",
    zValidator("json", z.object({ email: z.string().email(), reason: z.string().optional() })),
    async (c) => {
      const denied = requirePerm(c, "suppression", "create");
      if (denied) return denied;
      const { email, reason } = c.req.valid("json");
      await addSuppression(c.get("organizationId")!, email, reason ?? "manual");
      audit(c, "suppression.create", email);
      return c.json({ success: true }, 201);
    },
  )
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "suppression", "delete");
    if (denied) return denied;
    const ok = await removeSuppression(c.req.param("id"), c.get("organizationId")!);
    if (ok) audit(c, "suppression.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  });

const safeDomain = (d: Parameters<typeof buildRecords>[0]) => {
  const { dkimPrivateKey: _omit, ...rest } = d;
  return { ...rest, records: buildRecords(d) };
};

export const domainsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "domain", "read");
    if (denied) return denied;
    const list = await listDomains(c.get("organizationId")!);
    return c.json(list.map(safeDomain));
  })
  .post(
    "/",
    zValidator("json", z.object({ domain: z.string().min(3) })),
    async (c) => {
      const denied = requirePerm(c, "domain", "create");
      if (denied) return denied;
      try {
        const d = await createDomain(
          c.get("organizationId")!,
          c.req.valid("json").domain,
        );
        audit(c, "domain.create", d.domain);
        return c.json(safeDomain(d), 201);
      } catch (e) {
        // Drizzle wraps the pg error: the unique-violation code (23505) and the
        // "duplicate key" message live on e.cause, not the top-level error.
        const err = e as { code?: string; message?: string; cause?: unknown };
        const cause = err.cause as { code?: string; message?: string } | undefined;
        const code = cause?.code ?? err.code;
        const text = `${err.message ?? ""} ${cause?.message ?? ""}`;
        if (code === "23505" || /duplicate key|unique/i.test(text)) {
          return c.json({ error: "That domain is already added" }, 409);
        }
        throw e;
      }
    },
  )
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "domain", "read");
    if (denied) return denied;
    const d = await getDomain(c.req.param("id"), c.get("organizationId")!);
    if (!d) return c.json({ error: "Not found" }, 404);
    return c.json(safeDomain(d));
  })
  .post("/:id/verify", async (c) => {
    const denied = requirePerm(c, "domain", "create");
    if (denied) return denied;
    const d = await verifyDomain(c.req.param("id"), c.get("organizationId")!);
    if (!d) return c.json({ error: "Not found" }, 404);
    audit(c, "domain.verify", d.domain);
    return c.json(safeDomain(d));
  })
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "domain", "delete");
    if (denied) return denied;
    const ok = await deleteDomain(c.req.param("id"), c.get("organizationId")!);
    if (ok) audit(c, "domain.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  });

const webhookEvent = z.enum([
  "email.sent",
  "email.failed",
  "form.submission",
  "incoming.received",
]);

export const webhooksRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "webhook", "read");
    if (denied) return denied;
    return c.json(await listWebhooks(c.get("organizationId")!));
  })
  .post(
    "/",
    zValidator(
      "json",
      z.object({ url: z.string().url(), events: z.array(webhookEvent).min(1) }),
    ),
    async (c) => {
      const denied = requirePerm(c, "webhook", "create");
      if (denied) return denied;
      const w = await createWebhook(c.get("organizationId")!, c.req.valid("json"));
      audit(c, "webhook.create", w.url);
      return c.json(w, 201);
    },
  )
  .patch(
    "/:id",
    zValidator(
      "json",
      z.object({
        url: z.string().url().optional(),
        events: z.array(webhookEvent).optional(),
        enabled: z.boolean().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "webhook", "update");
      if (denied) return denied;
      const w = await updateWebhook(
        c.req.param("id"),
        c.get("organizationId")!,
        c.req.valid("json"),
      );
      if (!w) return c.json({ error: "Not found" }, 404);
      audit(c, "webhook.update", w.url);
      return c.json(w);
    },
  )
  .post("/:id/ping", async (c) => {
    const denied = requirePerm(c, "webhook", "update");
    if (denied) return denied;
    const w = await pingWebhook(c.req.param("id"), c.get("organizationId")!);
    if (!w) return c.json({ error: "Not found" }, 404);
    audit(c, "webhook.ping", w.url);
    return c.json(w);
  })
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "webhook", "delete");
    if (denied) return denied;
    const ok = await deleteWebhook(c.req.param("id"), c.get("organizationId")!);
    if (ok) audit(c, "webhook.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  });

export const analyticsRouter = new Hono<AppVariables>().get("/", async (c) => {
  const denied = requirePerm(c, "email", "read");
  if (denied) return denied;
  const days = Math.min(90, Math.max(7, Number(c.req.query("days") ?? 30)));
  return c.json(await getAnalytics(c.get("organizationId")!, days));
});

export const failedJobsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "email", "read");
    if (denied) return denied;
    const orgId = c.get("organizationId")!;
    const jobs = await mailQueue.getFailed(0, 100);
    const mine = jobs
      .filter((j) => j.data?.organizationId === orgId)
      .map((j) => ({
        id: j.id,
        to: j.data?.to ?? [],
        subject: j.data?.subject ?? "",
        failedReason: j.failedReason ?? "",
        attemptsMade: j.attemptsMade,
        timestamp: j.timestamp,
      }));
    return c.json(mine);
  })
  .post("/:id/retry", async (c) => {
    const denied = requirePerm(c, "email", "send");
    if (denied) return denied;
    const job = await mailQueue.getJob(c.req.param("id"));
    if (!job || job.data?.organizationId !== c.get("organizationId")) {
      return c.json({ error: "Not found" }, 404);
    }
    await job.retry();
    return c.json({ success: true });
  });

export const audiencesRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "audience", "read");
    if (denied) return denied;
    return c.json(await listAudiences(c.get("organizationId")!));
  })
  .post(
    "/",
    zValidator("json", z.object({ name: z.string().min(1) })),
    async (c) => {
      const denied = requirePerm(c, "audience", "create");
      if (denied) return denied;
      const a = await createAudience(c.get("organizationId")!, c.req.valid("json").name);
      audit(c, "audience.create", a.name);
      return c.json(a, 201);
    },
  )
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "audience", "read");
    if (denied) return denied;
    const a = await getAudience(c.req.param("id"), c.get("organizationId")!);
    if (!a) return c.json({ error: "Not found" }, 404);
    return c.json({ ...a, contacts: await listContacts(a.id) });
  })
  .delete("/:id", async (c) => {
    const denied = requirePerm(c, "audience", "delete");
    if (denied) return denied;
    const ok = await deleteAudience(c.req.param("id"), c.get("organizationId")!);
    if (ok) audit(c, "audience.delete", c.req.param("id"));
    return c.json({ success: ok }, ok ? 200 : 404);
  })
  .post(
    "/:id/contacts",
    zValidator(
      "json",
      z.object({
        contacts: z
          .array(
            z.object({
              email: z.string().email(),
              firstName: z.string().optional(),
              lastName: z.string().optional(),
            }),
          )
          .min(1),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "audience", "update");
      if (denied) return denied;
      const a = await getAudience(c.req.param("id"), c.get("organizationId")!);
      if (!a) return c.json({ error: "Not found" }, 404);
      const added = await addContacts(
        c.get("organizationId")!,
        a.id,
        c.req.valid("json").contacts,
      );
      audit(c, "audience.contacts.add", `${a.name} (+${added})`);
      return c.json({ added }, 201);
    },
  )
  .delete("/:id/contacts/:contactId", async (c) => {
    const denied = requirePerm(c, "audience", "update");
    if (denied) return denied;
    const orgId = c.get("organizationId")!;
    const audienceId = c.req.param("id");
    const contactId = c.req.param("contactId");
    // Validate the audience belongs to the org and that the contact actually
    // lives in *this* audience before deleting — mirrors the POST route and
    // scopes the delete to (contactId, audienceId) so a contact can't be removed
    // through an unrelated audience in the path.
    const a = await getAudience(audienceId, orgId);
    if (!a) return c.json({ error: "Not found" }, 404);
    const inAudience = (await listContacts(a.id)).some((ct) => ct.id === contactId);
    if (!inAudience) return c.json({ error: "Not found" }, 404);
    const ok = await removeContact(contactId, orgId);
    if (ok) audit(c, "audience.contacts.remove", `${a.name} → ${contactId}`);
    return c.json({ success: ok }, ok ? 200 : 404);
  });

export const broadcastsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "broadcast", "read");
    if (denied) return denied;
    return c.json(await listBroadcasts(c.get("organizationId")!));
  })
  .post(
    "/",
    zValidator(
      "json",
      z.object({
        audienceId: z.string().uuid(),
        templateId: z.string().uuid(),
        smtpConfigId: z.string().uuid().optional(),
        name: z.string().min(1),
        subject: z.string().min(1),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "broadcast", "create");
      if (denied) return denied;
      const orgId = c.get("organizationId")!;
      const body = c.req.valid("json");

      const audience = await getAudience(body.audienceId, orgId);
      if (!audience) return c.json({ error: "Audience not found" }, 404);

      const template = await getTemplate(body.templateId, orgId);
      if (!template) return c.json({ error: "Template not found" }, 404);

      const config = body.smtpConfigId
        ? await getSmtpConfigById(body.smtpConfigId)
        : await getDefaultSmtpConfig(orgId);
      if (!config || config.organizationId !== orgId) {
        return c.json({ error: "No SMTP connection available" }, 400);
      }

      const subscribed = await getSubscribedContacts(audience.id);
      // Drop suppressed addresses BEFORE we count or enqueue: they must not
      // inflate totalCount/sentCount and must never be sent to.
      const { allowed } = await filterSuppressed(
        orgId,
        subscribed.map((ct) => ct.email),
      );
      const allowedSet = new Set(allowed.map((e) => e.toLowerCase().trim()));
      const recipients = subscribed.filter((ct) =>
        allowedSet.has(ct.email.toLowerCase().trim()),
      );

      const broadcast = await createBroadcast(orgId, {
        audienceId: audience.id,
        smtpConfigId: config.id,
        templateId: template.id,
        name: body.name,
        subject: body.subject,
        totalCount: recipients.length,
      });

      const from = config.fromName
        ? `${config.fromName} <${config.fromAddress}>`
        : config.fromAddress;

      let sent = 0;
      let failed = 0;
      for (const contact of recipients) {
        const data = {
          email: contact.email,
          firstName: contact.firstName ?? "",
          lastName: contact.lastName ?? "",
          name: [contact.firstName, contact.lastName].filter(Boolean).join(" "),
        };
        const base =
          template.mode === "html"
            ? renderCustomHtml(template.html ?? "", data)
            : renderBlocks(template.blocks ?? [], {
                accent: template.accent ?? undefined,
                dark: template.theme === "dark",
                data,
              });
        const unsub = `<div style="text-align:center;padding:16px;font-size:11px;color:#94a3b8;font-family:sans-serif;">You're receiving this because you subscribed. <a href="${PUBLIC_BASE}/u/${signId(contact.id)}" style="color:#94a3b8;">Unsubscribe</a></div>`;
        const html = /<\/body>/i.test(base)
          ? base.replace(/<\/body>/i, `${unsub}</body>`)
          : base + unsub;

        try {
          await enqueueEmail({
            smtpConfigId: config.id,
            organizationId: orgId,
            from,
            to: [{ email: contact.email }],
            subject: renderCustomHtml(body.subject, data),
            html,
          });
          sent++;
        } catch {
          failed++;
        }
      }

      audit(
        c,
        "broadcast.send",
        `${broadcast.name} → ${sent} sent, ${failed} failed`,
      );

      // Only persist the terminal "sent" state when every enqueue succeeded.
      // On partial/total failure we leave the row untouched and report the real
      // outcome so a failed batch is never recorded as fully sent.
      if (failed === 0) {
        const completed = await completeBroadcast(broadcast.id, sent);
        return c.json(
          { ...(completed ?? broadcast), failedCount: failed },
          201,
        );
      }

      return c.json(
        {
          ...broadcast,
          status: sent > 0 ? "partial" : "failed",
          sentCount: sent,
          failedCount: failed,
        },
        201,
      );
    },
  );

export const auditRouter = new Hono<AppVariables>().get("/", async (c) => {
  if (!c.get("organizationId") || !c.get("role")) {
    return c.json({ error: "Authentication required" }, 401);
  }
  return c.json(await listAudit(c.get("organizationId")!, 50));
});

export const logsRouter = new Hono<AppVariables>().get("/", async (c) => {
  const denied = requirePerm(c, "email", "read");
  if (denied) return denied;
  const limit = Number(c.req.query("limit") ?? 100);
  return c.json(await listEmailLogs(c.get("organizationId")!, limit));
});
