import { Hono } from "hono";
import { zValidator } from "@hono/zod-validator";
import { z } from "zod";
import {
  listSmtpConfigs,
  getSmtpConfig,
  createSmtpConfig,
  updateSmtpConfig,
  deleteSmtpConfig,
  testSmtpConnection,
  testImapConnection,
  createApiToken,
  listApiTokens,
  deleteApiToken,
  getEmailLogsByConfig,
  sendMail,
} from "@workspace/mail-queue";
import type { AppVariables } from ".";
import { describeRoute } from "hono-openapi";
import { requirePerm } from "./org-context";
import { canGrantRole, isAppRole } from "@workspace/auth/permissions";

const smtpConfigsRouter = new Hono<AppVariables>()
  .get(
    "/",
    describeRoute({
      summary: "List SMTP configs",
      description: "Get all SMTP configurations for the authenticated user",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "List of SMTP configurations",
          content: {
            "application/json": {
              schema: {
                type: "array",
                items: { $ref: "#/components/schemas/SmtpConfig" },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "read");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const configs = await listSmtpConfigs(organizationId);
      return c.json(
        configs.map(({ password, imapPassword, ...rest }) => rest),
      );
    }
  )

  .get(
    "/:id",
    describeRoute({
      summary: "Get SMTP config",
      description: "Get a single SMTP configuration by ID",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "SMTP configuration",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SmtpConfig" },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "read");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const { password, imapPassword, ...safe } = config;
      return c.json(safe);
    }
  )

  .post(
    "/",
    describeRoute({
      summary: "Create SMTP config",
      description: "Create a new SMTP configuration",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "201": {
          description: "SMTP configuration created",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SmtpConfig" },
            },
          },
        },
        "401": { description: "Unauthorized" },
      },
    }),
    zValidator(
      "json",
      z.object({
        name: z.string().min(1),
        host: z.string().min(1),
        port: z.number().int().min(1).max(65535).default(587),
        username: z.string().min(1),
        password: z.string().min(1),
        fromAddress: z.string().email(),
        fromName: z.string().optional(),
        // Optional incoming mail (IMAP). Receive-enabled when imapHost is set.
        imapHost: z.string().optional(),
        imapPort: z.number().int().min(1).max(65535).optional(),
        imapUsername: z.string().optional(),
        imapPassword: z.string().optional(),
        imapSecure: z.boolean().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "create");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const data = c.req.valid("json");
      const config = await createSmtpConfig(organizationId, data);
      const { password, imapPassword, ...safe } = config;
      return c.json(safe, 201);
    }
  )

  .patch(
    "/:id",
    describeRoute({
      summary: "Update SMTP config",
      description: "Update an existing SMTP configuration",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "SMTP configuration updated",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SmtpConfig" },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
      },
    }),
    zValidator(
      "json",
      z.object({
        name: z.string().min(1).optional(),
        host: z.string().min(1).optional(),
        port: z.number().int().min(1).max(65535).optional(),
        username: z.string().min(1).optional(),
        password: z.string().min(1).optional(),
        fromAddress: z.string().email().optional(),
        fromName: z.string().optional(),
        isDefault: z.boolean().optional(),
        // IMAP — an empty-string host/username clears it (disables receiving).
        imapHost: z.string().optional(),
        imapPort: z.number().int().min(1).max(65535).nullable().optional(),
        imapUsername: z.string().optional(),
        imapPassword: z.string().optional(),
        imapSecure: z.boolean().nullable().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "update");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await updateSmtpConfig(
        c.req.param("id"),
        organizationId,
        c.req.valid("json"),
      );
      if (!config) return c.json({ error: "Not found" }, 404);

      const { password, imapPassword, ...safe } = config;
      return c.json(safe);
    }
  )

  .delete(
    "/:id",
    describeRoute({
      summary: "Delete SMTP config",
      description: "Delete an SMTP configuration",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "SMTP configuration deleted",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: { success: { type: "boolean" } },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "delete");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const deleted = await deleteSmtpConfig(c.req.param("id"), organizationId);
      if (!deleted) return c.json({ error: "Not found" }, 404);
      return c.json({ success: true });
    }
  )

  .post(
    "/:id/test",
    describeRoute({
      summary: "Test SMTP connection",
      description: "Test the SMTP connection for a configuration",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "Connection successful",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  success: { type: "boolean" },
                  message: { type: "string" },
                },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
        "422": {
          description: "Connection failed",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  success: { type: "boolean" },
                  error: { type: "string" },
                },
              },
            },
          },
        },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "test");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const result = await testSmtpConnection(config);
      return c.json(result, result.success ? 200 : 422);
    }
  )

  // Test IMAP creds typed into the NEW form (not yet saved).
  .post(
    "/test-imap",
    zValidator(
      "json",
      z.object({
        imapHost: z.string().min(1),
        imapPort: z.number().int().min(1).max(65535).optional(),
        imapSecure: z.boolean().optional(),
        imapUsername: z.string().optional(),
        imapPassword: z.string().min(1),
        username: z.string().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "test");
      if (denied) return denied;
      const b = c.req.valid("json");
      const result = await testImapConnection({
        imapHost: b.imapHost,
        imapPort: b.imapPort ?? null,
        imapSecure: b.imapSecure ?? null,
        imapUsername: b.imapUsername ?? null,
        imapPassword: b.imapPassword,
        username: b.username ?? "",
      });
      return c.json(result, result.success ? 200 : 422);
    },
  )

  // Test the IMAP creds already stored on a saved config (edit page).
  .post("/:id/test-imap", async (c) => {
    const denied = requirePerm(c, "smtpConfig", "test");
    if (denied) return denied;
    const config = await getSmtpConfig(c.req.param("id"), c.get("organizationId")!);
    if (!config) return c.json({ error: "Not found" }, 404);
    if (!config.imapHost) {
      return c.json({ success: false, error: "No IMAP mailbox configured" }, 422);
    }
    const result = await testImapConnection(config);
    return c.json(result, result.success ? 200 : 422);
  })

  .post(
    "/:id/test-send",
    describeRoute({
      summary: "Send test email",
      description: "Send a test email using the SMTP configuration",
      tags: ["SMTP Configs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "Test email sent",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  success: { type: "boolean" },
                  messageId: { type: "string" },
                },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
        "422": {
          description: "Failed to send email",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  success: { type: "boolean" },
                  error: { type: "string" },
                },
              },
            },
          },
        },
      },
    }),
    zValidator(
      "json",
      z.object({
        to: z.string().email(),
        subject: z.string().optional().default("Test email from LaunchMail"),
        body: z
          .string()
          .optional()
          .default(
            "<p>This is a test email to verify your SMTP configuration.</p>",
          ),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "smtpConfig", "test");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const data = c.req.valid("json");

      try {
        const result = await sendMail({
          from: config.fromName
            ? `${config.fromName} <${config.fromAddress}>`
            : config.fromAddress,
          to: [{ email: data.to }],
          subject: data.subject,
          html: data.body,
          smtpConfig: config,
        });

        return c.json({ success: true, messageId: result.messageId });
      } catch (err) {
        return c.json(
          {
            success: false,
            error: err instanceof Error ? err.message : String(err),
          },
          422,
        );
      }
    }
  )

  .get(
    "/:id/tokens",
    describeRoute({
      summary: "List API tokens",
      description: "Get all API tokens for an SMTP configuration",
      tags: ["API Tokens"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "List of API tokens",
          content: {
            "application/json": {
              schema: {
                type: "array",
                items: { $ref: "#/components/schemas/ApiToken" },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "SMTP config not found" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "apiKey", "read");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const tokens = await listApiTokens(organizationId);
      const configTokens = tokens.filter((t) => t.smtpConfigId === config.id);
      return c.json(
        configTokens.map(({ tokenHash, ...rest }) => rest),
      );
    }
  )

  .post(
    "/:id/tokens",
    describeRoute({
      summary: "Create API token",
      description:
        "Create a new API token for an SMTP configuration. The token is only returned once!",
      tags: ["API Tokens"],
      security: [{ CookieAuth: [] }],
      responses: {
        "201": {
          description: "API token created",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  id: { type: "string", format: "uuid" },
                  name: { type: "string" },
                  role: {
                    type: "string",
                    enum: ["admin", "writer", "reader"],
                  },
                  tokenPrefix: { type: "string" },
                  smtpConfigId: { type: "string", format: "uuid" },
                  createdAt: { type: "string", format: "date-time" },
                  token: {
                    type: "string",
                    description: "The plaintext token - only returned once!",
                  },
                },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "SMTP config not found" },
      },
    }),
    zValidator(
      "json",
      z.object({
        name: z.string().min(1),
        // Default to "writer" when the caller omits role (e.g. the dashboard
        // token manager only sends a name) so token creation does not 400.
        role: z.enum(["admin", "writer", "reader"]).optional().default("writer"),
        expiresAt: z.string().datetime().optional(),
      }),
    ),
    async (c) => {
      const denied = requirePerm(c, "apiKey", "create");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;
      const callerRole = c.get("role")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const data = c.req.valid("json");
      if (!isAppRole(data.role) || !canGrantRole(callerRole, data.role)) {
        return c.json(
          { error: "Cannot grant a role above your own" },
          403,
        );
      }

      const token = await createApiToken(organizationId, {
        name: data.name,
        role: data.role,
        smtpConfigId: config.id,
        expiresAt: data.expiresAt ? new Date(data.expiresAt) : undefined,
        createdByUserId: c.get("user")?.id,
      });

      return c.json(
        {
          id: token.id,
          name: token.name,
          role: token.role,
          tokenPrefix: token.tokenPrefix,
          smtpConfigId: token.smtpConfigId,
          createdAt: token.createdAt,
          token: token.plaintext,
        },
        201,
      );
    }
  )

  .delete(
    "/:id/tokens/:tokenId",
    describeRoute({
      summary: "Delete API token",
      description: "Delete an API token",
      tags: ["API Tokens"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "API token deleted",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: { success: { type: "boolean" } },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "Not found" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "apiKey", "revoke");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const deleted = await deleteApiToken(c.req.param("tokenId"), organizationId);
      if (!deleted) return c.json({ error: "Not found" }, 404);
      return c.json({ success: true });
    }
  )

  .get(
    "/:id/logs",
    describeRoute({
      summary: "Get email logs",
      description: "Get email logs for an SMTP configuration",
      tags: ["Email Logs"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "List of email logs",
          content: {
            "application/json": {
              schema: {
                type: "array",
                items: { $ref: "#/components/schemas/EmailLog" },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
        "404": { description: "SMTP config not found" },
      },
    }),
    async (c) => {
      const denied = requirePerm(c, "email", "read");
      if (denied) return denied;
      const organizationId = c.get("organizationId")!;

      const config = await getSmtpConfig(c.req.param("id"), organizationId);
      if (!config) return c.json({ error: "Not found" }, 404);

      const logs = await getEmailLogsByConfig(config.id);
      return c.json(logs);
    }
  );

export default smtpConfigsRouter;
