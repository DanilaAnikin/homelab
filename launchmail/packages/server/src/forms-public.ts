import { Hono, type Context } from "hono";
import { bodyLimit } from "hono/body-limit";
import {
  getFormByToken,
  recordSubmission,
  getDefaultSmtpConfig,
  getSmtpConfigById,
  enqueueEmail,
  getRedis,
  dispatchEvent,
  recordOpen,
  recordClick,
  unsubscribeContact,
  getTemplate,
  parseSignedId,
  verify,
} from "@workspace/mail-queue";
import {
  renderForm,
  getDesign,
  DEFAULT_DESIGN_KEY,
  renderBlocks,
  renderCustomHtml,
  escapeHtml,
} from "@workspace/templates";
import { db } from "@workspace/db";
import { invitation, organization, user } from "@workspace/db/schemas";
import { eq } from "drizzle-orm";

const THANK_YOU_HTML = `<!doctype html><html><head><meta charset="utf-8"><title>Thanks</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;background:#f4f5f7;">
<div style="text-align:center;padding:32px;"><h1 style="font-size:22px;">Thanks!</h1><p style="color:#6b7280;">Your submission was received.</p></div>
</body></html>`;

// Public form submission limits.
const MAX_FORM_BODY_BYTES = 256 * 1024; // 256KB
const MAX_FORM_FIELDS = 100;
const MAX_FIELD_VALUE_LENGTH = 8 * 1024; // 8KB per value

// Auto-responder abuse controls (independent of client IP).
const AUTORESPONDER_DAILY_CAP = 500; // per form, per day
const AUTORESPONDER_DEDUPE_TTL = 3600 * 24; // one confirmation per recipient/day

function corsHeaders(
  origin: string | null,
  allowed?: string[],
): Record<string, string> {
  const allowOrigin =
    !allowed || allowed.length === 0
      ? "*"
      : origin && allowed.includes(origin)
        ? origin
        : (allowed[0] ?? "*");
  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

const TRANSPARENT_GIF = Buffer.from(
  "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
  "base64",
);

const PIXEL_HEADERS = {
  "Content-Type": "image/gif",
  "Cache-Control": "no-store, no-cache, must-revalidate, private",
  Pragma: "no-cache",
} as const;

// Derive the client IP from X-Forwarded-For using a trusted-proxy hop count
// rather than blindly trusting the (spoofable) left-most value. TRUSTED_PROXY_COUNT
// is the number of proxies we operate in front of the app; the client IP is the
// entry that many hops from the right of the header — the position our own proxy
// appended and that a client cannot forge.
function clientIp(c: Context): string {
  const trustedHops = Number(process.env.TRUSTED_PROXY_COUNT ?? "0");
  const xff = c.req.header("x-forwarded-for");
  if (Number.isFinite(trustedHops) && trustedHops > 0 && xff) {
    const parts = xff
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
    if (parts.length > 0) {
      const idx = parts.length - trustedHops;
      const ip = parts[idx >= 0 ? idx : 0];
      if (ip) return ip;
    }
  }
  // No trusted proxy configured (or header absent): do not trust XFF. Fall back
  // to a direct-connection hint header, else a constant bucket so the rate limit
  // still applies globally rather than being trivially bypassed per spoofed IP.
  return c.req.header("x-real-ip")?.trim() || "global";
}

export const formsPublicApp = new Hono()
  .get("/t/o/:id", async (c) => {
    const token = c.req.param("id").replace(/\.gif$/i, "");
    const logId = parseSignedId(token);
    if (logId) await recordOpen(logId);
    // Always return the pixel so a forged/expired token doesn't leak validity.
    return c.body(TRANSPARENT_GIF, 200, PIXEL_HEADERS);
  })
  .get("/t/c/:id", async (c) => {
    const logId = parseSignedId(c.req.param("id"));
    const url = c.req.query("u");
    const sig = c.req.query("s");
    // The destination URL is HMAC-signed at injection time; reject tampering.
    if (!logId || !url || !sig || !verify(url, sig)) {
      return c.text("Invalid or expired link", 400);
    }
    if (!/^https?:\/\//i.test(url)) {
      return c.text("Invalid or expired link", 400);
    }
    await recordClick(logId);
    return c.redirect(url, 302);
  })
  .get("/u/:token", async (c) => {
    const contactId = parseSignedId(c.req.param("token"));
    if (!contactId) {
      return c.html(unsubscribePage("This link is no longer valid.", null), 400);
    }
    // GET must not mutate: render a confirmation page with a one-click POST form.
    const token = c.req.param("token");
    return c.html(
      `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Unsubscribe</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;background:#f4f5f7;">
<div style="text-align:center;padding:32px;max-width:420px;"><h1 style="font-size:20px;color:#0f172a;">Unsubscribe</h1><p style="color:#64748b;font-size:14px;">Click below to stop receiving these emails.</p>
<form method="post" action="/u/${escapeHtml(token)}"><button type="submit" style="margin-top:8px;padding:10px 20px;font-size:14px;font-weight:600;color:#ffffff;background:#0f172a;border:none;border-radius:8px;cursor:pointer;">Unsubscribe</button></form>
</div></body></html>`,
    );
  })
  .post("/u/:token", async (c) => {
    const contactId = parseSignedId(c.req.param("token"));
    if (!contactId) {
      return c.html(unsubscribePage("This link is no longer valid.", null), 400);
    }
    // unsubscribeContact is scoped to the org derived from the validated contact.
    const contact = await unsubscribeContact(contactId).catch(() => null);
    const msg = contact
      ? `${contact.email} won't receive further emails.`
      : "This link is no longer valid.";
    return c.html(unsubscribePage(msg, contact ? "Unsubscribed" : null));
  })
  .get("/public/invitation/:id", async (c) => {
    const [row] = await db
      .select({
        id: invitation.id,
        email: invitation.email,
        role: invitation.role,
        status: invitation.status,
        expiresAt: invitation.expiresAt,
        orgName: organization.name,
        inviterName: user.name,
      })
      .from(invitation)
      .innerJoin(organization, eq(invitation.organizationId, organization.id))
      .innerJoin(user, eq(invitation.inviterId, user.id))
      .where(eq(invitation.id, c.req.param("id")));
    if (!row) return c.json({ error: "Not found" }, 404);
    return c.json(row);
  })
  .options("/f/:token", (c) =>
    c.body(null, 204, corsHeaders(c.req.header("origin") ?? null)),
  )
  .post(
    "/f/:token",
    bodyLimit({
      maxSize: MAX_FORM_BODY_BYTES,
      onError: (c) =>
        c.json(
          { error: "Submission too large" },
          413,
          corsHeaders(c.req.header("origin") ?? null),
        ),
    }),
    async (c) => {
      const token = c.req.param("token");
      const origin = c.req.header("origin") ?? null;
      const form = await getFormByToken(token);

      if (!form || !form.enabled) {
        return c.json({ error: "Form not found" }, 404, corsHeaders(origin));
      }

      const settings = form.settings ?? {};
      const cors = corsHeaders(origin, settings.allowedOrigins);

      if (
        settings.allowedOrigins &&
        settings.allowedOrigins.length > 0 &&
        origin &&
        !settings.allowedOrigins.includes(origin)
      ) {
        return c.json({ error: "Origin not allowed" }, 403, cors);
      }

      // Rate-limit before parsing the body where possible. Derive the client IP
      // from a trusted-proxy config (not the spoofable left-most XFF) and fail
      // CLOSED (conservative 429) if the rate-limit backend is unavailable.
      const maxPerHour = settings.maxPerHour ?? 200;
      const ip = clientIp(c);
      try {
        const redis = getRedis();
        const key = `formrl:${form.id}:${ip}`;
        const count = await redis.incr(key);
        if (count === 1) await redis.expire(key, 3600);
        if (count > maxPerHour) {
          return c.json(
            { error: "Too many submissions, try again later" },
            429,
            cors,
          );
        }
      } catch {
        // Rate-limit backend unavailable — fail CLOSED. Without a working limiter
        // we cannot bound abuse, so reject rather than silently disabling it.
        return c.json(
          { error: "Service temporarily unavailable, try again later" },
          503,
          cors,
        );
      }

      const contentType = c.req.header("content-type") ?? "";
      const wantsJson =
        contentType.includes("application/json") ||
        (c.req.header("accept") ?? "").includes("application/json") ||
        c.req.header("x-requested-with") === "XMLHttpRequest";

      const data: Record<string, string> = {};
      let fieldCount = 0;
      let overSized = false;
      const addField = (k: string, raw: string): boolean => {
        if (fieldCount >= MAX_FORM_FIELDS) return false;
        if (raw.length > MAX_FIELD_VALUE_LENGTH) {
          overSized = true;
          return false;
        }
        data[k] = raw;
        fieldCount++;
        return true;
      };

      if (contentType.includes("application/json")) {
        const body = (await c.req.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        for (const [k, v] of Object.entries(body)) {
          const raw = typeof v === "string" ? v : JSON.stringify(v);
          if (!addField(k, raw)) break;
        }
      } else {
        const body = await c.req.parseBody().catch(() => ({}));
        for (const [k, v] of Object.entries(body)) {
          const raw = typeof v === "string" ? v : (v as File).name;
          if (!addField(k, raw)) break;
        }
      }

      if (overSized) {
        return c.json(
          { error: "A submitted field exceeds the maximum allowed length" },
          413,
          cors,
        );
      }

      if (settings.honeypot !== false && data["_gotcha"]) {
        return respond(c, wantsJson, settings.redirectUrl, cors, null);
      }

      const meta = {
        ip,
        userAgent: c.req.header("user-agent") ?? undefined,
        referer: c.req.header("referer") ?? undefined,
      };
      if (settings.storeSubmissions !== false) {
        await recordSubmission(form.id, data, meta).catch(() => undefined);
      }

      await dispatchEvent(form.organizationId, "form.submission", {
        formId: form.id,
        formName: form.name,
        data,
      });

      const config = await (form.smtpConfigId
        ? getSmtpConfigById(form.smtpConfigId)
        : getDefaultSmtpConfig(form.organizationId)
      ).catch(() => null);

      if (config) {
        // Submitter-controlled values interpolated into the (org-owned) subject
        // template are HTML-escaped to prevent injection downstream.
        const interpSubject = (s: string) =>
          s.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_m, k) =>
            escapeHtml(data[k] ?? ""),
          );

        let rendered: { subject: string; html: string; text: string } | null =
          null;

        // A user-built template (from the Template Builder) takes precedence.
        if (form.templateId) {
          const tpl = await getTemplate(form.templateId, form.organizationId);
          if (tpl) {
            const html =
              tpl.mode === "html"
                ? renderCustomHtml(tpl.html ?? "", data)
                : renderBlocks(tpl.blocks ?? [], {
                    accent: tpl.accent ?? undefined,
                    dark: tpl.theme === "dark",
                    brandName: form.branding?.brandName,
                    data,
                  });
            rendered = {
              subject: interpSubject(
                form.subject || tpl.subject || `${form.name} submission`,
              ),
              html,
              text: Object.entries(data)
                .filter(([k]) => !k.startsWith("_"))
                .map(([k, v]) => `${k}: ${v}`)
                .join("\n"),
            };
          }
        }

        // Fall back to the built-in form design.
        if (!rendered) {
          const design =
            getDesign(form.templateKey) ?? getDesign(DEFAULT_DESIGN_KEY)!;
          rendered = renderForm({
            design,
            branding: form.branding,
            subjectTemplate: form.subject,
            customHtml: form.customHtml,
            data,
            submittedAt: new Date().toUTCString(),
          });
        }

        const replyTo = form.replyToField ? data[form.replyToField] : undefined;
        await enqueueEmail({
          smtpConfigId: config.id,
          organizationId: form.organizationId,
          from: config.fromName
            ? `${config.fromName} <${config.fromAddress}>`
            : config.fromAddress,
          to: form.recipients.map((email) => ({ email })),
          replyTo:
            replyTo && /^\S+@\S+\.\S+$/.test(replyTo) ? replyTo : undefined,
          subject: rendered.subject,
          html: rendered.html,
          text: rendered.text,
        }).catch(() => undefined);

        // Auto-responder: confirmation email back to the submitter. This is an
        // open-relay/amplification surface, so it is gated by several abuse
        // controls that do NOT depend on a client-supplied header:
        //   * an intact honeypot (handled above — a tripped honeypot returns early)
        //   * a per-form daily volume cap
        //   * per-recipient dedupe (one confirmation per address per day)
        //   * fail CLOSED if the dedupe/cap backend is unavailable
        const submitterEmail = data[form.replyToField || "email"];
        if (
          settings.autoRespond &&
          submitterEmail &&
          /^\S+@\S+\.\S+$/.test(submitterEmail)
        ) {
          let allowed = false;
          try {
            const redis = getRedis();
            const normalized = submitterEmail.trim().toLowerCase();
            const day = new Date().toISOString().slice(0, 10);
            const dedupeKey = `formar:dedupe:${form.id}:${day}:${normalized}`;
            // Dedupe first: only the first request for this recipient/day proceeds.
            const firstSeen = await redis.set(
              dedupeKey,
              "1",
              "EX",
              AUTORESPONDER_DEDUPE_TTL,
              "NX",
            );
            if (firstSeen) {
              const capKey = `formar:cap:${form.id}:${day}`;
              const sent = await redis.incr(capKey);
              if (sent === 1) await redis.expire(capKey, AUTORESPONDER_DEDUPE_TTL);
              allowed = sent <= AUTORESPONDER_DAILY_CAP;
            }
          } catch {
            // Backend unavailable — fail CLOSED: do not send the auto-responder.
            allowed = false;
          }

          if (allowed) {
            const interp = (s: string) =>
              s.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_m, k) =>
                escapeHtml(data[k] ?? ""),
              );
            const msg = interp(
              settings.autoRespondMessage ||
                "Thanks for your submission — we'll be in touch soon.",
            );
            const subj = interp(
              settings.autoRespondSubject || "Thanks for reaching out",
            );
            const arHtml = `<!doctype html><html><body style="margin:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:40px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center"><table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background:#ffffff;border:1px solid #e8eaef;border-radius:14px;"><tr><td style="padding:32px 40px;font-size:15px;line-height:1.65;color:#0f172a;white-space:pre-wrap;">${msg}</td></tr></table></td></tr></table></body></html>`;
            await enqueueEmail({
              smtpConfigId: config.id,
              organizationId: form.organizationId,
              from: config.fromName
                ? `${config.fromName} <${config.fromAddress}>`
                : config.fromAddress,
              to: [{ email: submitterEmail }],
              subject: subj,
              html: arHtml,
            }).catch(() => undefined);
          }
        }
      }

      return respond(c, wantsJson, settings.redirectUrl, cors, form.id);
    },
  );

function unsubscribePage(message: string, heading: string | null): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${heading ? "Unsubscribed" : "Unsubscribe"}</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;background:#f4f5f7;">
<div style="text-align:center;padding:32px;max-width:420px;"><h1 style="font-size:20px;color:#0f172a;">${escapeHtml(heading ?? "Unsubscribe")}</h1><p style="color:#64748b;font-size:14px;">${escapeHtml(message)}</p></div>
</body></html>`;
}

function respond(
  c: Context,
  wantsJson: boolean,
  redirectUrl: string | undefined,
  cors: Record<string, string>,
  id: string | null,
) {
  if (!wantsJson && redirectUrl) {
    return c.redirect(redirectUrl, 303);
  }
  if (!wantsJson) {
    return c.html(THANK_YOU_HTML, 200, cors);
  }
  return c.json({ ok: true, id }, 200, cors);
}
