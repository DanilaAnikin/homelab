import { Hono } from "hono";
import { z } from "zod";
import { zValidator } from "@hono/zod-validator";
import {
  enqueueEmail,
  sendEmailSchema,
  getSmtpConfigById,
  getDefaultSmtpConfig,
} from "@workspace/mail-queue";
import { hasPermission } from "@workspace/auth/permissions";
import type { AppVariables } from ".";
import { describeRoute } from "hono-openapi";

// Extend the base send schema with an optional reply-to address (CONTRACT #6).
const sendMailRequestSchema = sendEmailSchema.extend({
  replyTo: z.string().email().optional(),
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

      const config = boundConfigId
        ? await getSmtpConfigById(boundConfigId)
        : await getDefaultSmtpConfig(organizationId);
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
