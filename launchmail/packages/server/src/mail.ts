import { Hono } from "hono";
import { z } from "zod";
import { zValidator } from "@hono/zod-validator";
import {
  enqueueEmail,
  sendEmailSchema,
  getSmtpConfigById,
  getDefaultSmtpConfig,
  getSmtpConfigByFromDomain,
} from "@workspace/mail-queue";
import { hasPermission } from "@workspace/auth/permissions";
import type { AppVariables } from ".";
import { describeRoute } from "hono-openapi";

// Extend the base send schema with an optional reply-to address (CONTRACT #6).
const sendMailRequestSchema = sendEmailSchema.extend({
  replyTo: z.string().email().optional(),
  // Per-request SMTP config selection (unbound tokens): pick which of the org's
  // configs / sender mailboxes to send from. Without this in the schema Zod
  // strips it and the send silently falls back to the org default config.
  smtpConfigId: z.string().uuid().optional(),
});

const mailRouter = new Hono<AppVariables>()
  .post(
    "/send",
    describeRoute({
      summary: "Send email",
      description:
        "Enqueue an email for delivery. Uses the API token's SMTP config if Bearer token is provided, otherwise falls back to the authenticated user's default config.",
      tags: ["Mail"],
      security: [{ BearerAuth: [] }, { CookieAuth: [] }],
      requestBody: {
        required: true,
        content: {
          "application/json": {
            schema: { $ref: "#/components/schemas/SendEmailRequest" },
          },
        },
      },
      responses: {
        "201": {
          description:
            "Email accepted. status is \"scheduled\" only when sendAt is a future timestamp (scheduledAt is then set); otherwise it is queued for immediate delivery and scheduledAt is null.",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  id: { type: "string", format: "uuid" },
                  status: {
                    type: "string",
                    enum: ["queued", "scheduled"],
                    example: "queued",
                  },
                  smtpConfigId: { type: "string", format: "uuid" },
                  scheduledAt: {
                    type: ["string", "null"],
                    format: "date-time",
                  },
                  createdAt: { type: "string", format: "date-time" },
                },
              },
            },
          },
        },
        "400": {
          description: "No SMTP config found or invalid request",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: { error: { type: "string" } },
              },
            },
          },
        },
        "401": {
          description: "Authentication required",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: { error: { type: "string" } },
              },
            },
          },
        },
        "403": {
          description: "Insufficient permissions to send",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: { error: { type: "string" } },
              },
            },
          },
        },
      },
    }),
    zValidator("json", sendMailRequestSchema),
    async (c) => {
      const data = c.req.valid("json");
      const organizationId = c.get("organizationId");
      const role = c.get("role");
      const user = c.get("user");
      const boundConfigId = c.get("apiTokenSmtpConfigId");

      if (!organizationId || !role) {
        return c.json(
          {
            error:
              "Authentication required. Provide a Bearer API token or sign in.",
          },
          401,
        );
      }
      if (!hasPermission(role, "email", "send")) {
        return c.json({ error: "Insufficient permissions to send" }, 403);
      }

      // A token bound to a specific SMTP config is locked to it (can't spoof
      // other senders). An unbound token may pick any of its org's configs
      // per-request via `smtpConfigId`, falling back to the org default. This
      // lets one token send from every per-domain mailbox (Lokwave email bot).
      const chosenConfigId = boundConfigId ?? data.smtpConfigId;

      // Doména požadovaného odesílatele. Bere se z "Jméno <adresa>" i z holé adresy.
      const requestedDomain = (data.from ?? "")
        .replace(/^.*</, "")
        .replace(/>.*$/, "")
        .split("@")
        .pop()
        ?.trim()
        .toLowerCase();

      // Když volající neurčí konfiguraci, VYBERE SE PODLE DOMÉNY ODESÍLATELE,
      // ne podle org defaultu. Default jako tichá záchrana byl přesně ta chyba:
      // appka LokWave poslala mail s From `noreply@dentallocal.cz`, LaunchMail
      // sáhl po výchozí konfiguraci `contact@freio.cz` a Seznam viditelného
      // odesílatele přepsal na freio. Mail tvrdil jedno a chodil odjinud.
      const config = chosenConfigId
        ? await getSmtpConfigById(chosenConfigId)
        : (requestedDomain
            ? await getSmtpConfigByFromDomain(organizationId, requestedDomain)
            : null) ?? (await getDefaultSmtpConfig(organizationId));
      if (!config || config.organizationId !== organizationId) {
        return c.json(
          {
            error:
              "No SMTP config available for this organization. Create one in the dashboard.",
          },
          400,
        );
      }

      let from = data.from;
      if (!from || from === "") {
        from = config.fromName
          ? `${config.fromName} <${config.fromAddress}>`
          : config.fromAddress;
      }

      // POJISTKA: odesílatel a schránka se nikdy nesmí rozejít. Když se doména
      // From liší od domény zvolené konfigurace (typicky token natvrdo vázaný
      // na jinou schránku, nebo doména bez vlastní konfigurace), přepíšeme
      // adresu na tu, kterou schránka opravdu má — zobrazované jméno necháme.
      // Jinak by odešel mail, který o sobě lže, a poskytovatel ho stejně
      // přepíše nebo odmítne kvůli SPF/DKIM.
      const finalDomain = from
        .replace(/^.*</, "")
        .replace(/>.*$/, "")
        .split("@")
        .pop()
        ?.trim()
        .toLowerCase();
      const configDomain = config.fromAddress.split("@").pop()?.toLowerCase();
      if (finalDomain && configDomain && finalDomain !== configDomain) {
        const display = from.includes("<")
          ? from.slice(0, from.indexOf("<")).trim().replace(/^"|"$/g, "")
          : (config.fromName ?? "");
        from = display ? `${display} <${config.fromAddress}>` : config.fromAddress;
        console.warn(
          `[mail] From doména ${finalDomain} nesouhlasí se schránkou ${configDomain}; ` +
            `odesílatel přepsán na ${config.fromAddress}`,
        );
      }

      // A send is only genuinely "scheduled" when sendAt parses to a real
      // future timestamp — matching the delay logic in enqueueEmail. A missing,
      // invalid, or past sendAt enqueues immediately and reports "queued".
      let scheduledAt: string | null = null;
      if (data.sendAt) {
        const ts = new Date(data.sendAt).getTime();
        if (!Number.isNaN(ts) && ts > Date.now()) {
          scheduledAt = new Date(ts).toISOString();
        }
      }

      const job = await enqueueEmail(
        {
          smtpConfigId: config.id,
          organizationId,
          userId: user?.id ?? null,
          from,
          to: data.to,
          cc: data.cc,
          bcc: data.bcc,
          replyTo: data.replyTo,
          subject: data.subject,
          html: data.html,
          text: data.text,
          attachments: data.attachments,
          inReplyTo: data.inReplyTo,
          references: data.references,
          clientReference: data.clientReference,
          clientType: data.clientType,
          headers: data.headers,
        },
        { sendAt: data.sendAt },
      );

      return c.json(
        {
          id: job.id,
          status: scheduledAt ? "scheduled" : "queued",
          smtpConfigId: config.id,
          scheduledAt,
          createdAt: new Date().toISOString(),
        },
        201,
      );
    }
  );

export default mailRouter;
