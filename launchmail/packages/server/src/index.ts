import { auth } from "@workspace/auth";
import { Hono } from "hono";
import { authMiddleware } from "./auth-middleware";
import { apiTokenMiddleware } from "./api-token-middleware";
import helloRouter from "./hello";
import mailRouter from "./mail";
import smtpConfigsRouter from "./smtp-configs";
import incomingEmailsRouter from "./incoming-emails";
import sentEmailsRouter from "./sent-emails";
import queueStatsRouter from "./queue-stats";
import {
  formsRouter,
  apiKeysRouter,
  logsRouter,
  templatesRouter,
  suppressionsRouter,
  domainsRouter,
  webhooksRouter,
  analyticsRouter,
  failedJobsRouter,
  audiencesRouter,
  broadcastsRouter,
  auditRouter,
} from "./management";
import { ensurePersonalOrg } from "./org-context";
import { cors } from "hono/cors";
import { Scalar } from "@scalar/hono-api-reference";
import { openAPIRouteHandler } from "hono-openapi";

export type AppVariables = {
  Variables: {
    user: typeof auth.$Infer.Session.user | null;
    session: typeof auth.$Infer.Session.session | null;
    organizationId: string | null;
    role: import("@workspace/auth/permissions").AppRole | null;
    apiTokenSmtpConfigId: string | null;
    apiTokenName: string | null;
  };
};

const webOrigins = (
  process.env.WEB_ORIGIN ?? "http://localhost:3000,http://localhost:5000"
)
  .split(",")
  .map((o) => o.trim())
  .filter(Boolean);

const app = new Hono<AppVariables>()
  .basePath("/api")
  .use(
    "*",
    cors({
      origin: webOrigins,
      allowHeaders: ["Content-Type", "Authorization"],
      allowMethods: ["POST", "GET", "HEAD", "PATCH", "PUT", "DELETE", "OPTIONS"],
      // set-auth-token carries the Bearer session token; the browser auth
      // client must be allowed to read it cross-origin.
      exposeHeaders: ["Content-Length", "set-auth-token"],
      maxAge: 600,
      credentials: true,
    }),
  )
  .use("*", authMiddleware)
  .use("*", apiTokenMiddleware)
  .route("/hello", helloRouter)
  .route("/mail", mailRouter)
  .route("/smtp-configs", smtpConfigsRouter)
  .route("/incoming-emails", incomingEmailsRouter)
  .route("/sent-emails", sentEmailsRouter)
  .route("/queue", queueStatsRouter)
  .route("/forms", formsRouter)
  .route("/templates", templatesRouter)
  .route("/suppressions", suppressionsRouter)
  .route("/domains", domainsRouter)
  .route("/webhooks", webhooksRouter)
  .route("/analytics", analyticsRouter)
  .route("/failed", failedJobsRouter)
  .route("/audiences", audiencesRouter)
  .route("/broadcasts", broadcastsRouter)
  .route("/audit", auditRouter)
  .route("/api-keys", apiKeysRouter)
  .route("/logs", logsRouter)
  .get("/me", async (c) => {
    let organizationId = c.get("organizationId");
    let role = c.get("role");
    const user = c.get("user");

    // A signed-in user with no org gets a personal workspace on first dashboard
    // load. Invited users accept first (gaining membership) and never reach here
    // without one, so they don't get a spurious personal org.
    if ((!organizationId || !role) && user) {
      const ensured = await ensurePersonalOrg(user.id);
      organizationId = ensured.organizationId;
      role = ensured.role;
    }

    if (!organizationId || !role) {
      return c.json({ error: "Authentication required" }, 401);
    }
    return c.json({
      organizationId,
      role,
      authKind: c.get("apiTokenName") ? "api_key" : "session",
    });
  });

app.get(
  "/openapi.json",
  openAPIRouteHandler(app, {
    documentation: {
      info: {
        title: "LaunchMail API",
        version: "1.0.0",
        description:
          "API for sending emails via configured SMTP servers. Authenticate via session cookie or Bearer API token.",
      },
      tags: [
        { name: "Health", description: "Health check endpoints" },
        { name: "Mail", description: "Send emails via the queue" },
        { name: "SMTP Configs", description: "Manage SMTP configurations" },
        { name: "API Tokens", description: "Manage API tokens for SMTP configs" },
        { name: "Queue", description: "Mail queue statistics" },
        { name: "Email Logs", description: "View email delivery logs" },
      ],
      components: {
        securitySchemes: {
          BearerAuth: {
            type: "http",
            scheme: "bearer",
            description: "API token for programmatic access",
          },
          CookieAuth: {
            type: "apiKey",
            in: "cookie",
            name: "better-auth.session_token",
            description: "Session cookie from Better Auth",
          },
        },
        schemas: {
          SmtpConfig: {
            type: "object",
            properties: {
              id: { type: "string", format: "uuid" },
              name: { type: "string" },
              host: { type: "string" },
              port: { type: "integer" },
              username: { type: "string" },
              fromAddress: { type: "string", format: "email" },
              fromName: { type: "string" },
              isDefault: { type: "boolean" },
              userId: { type: ["string", "null"], format: "uuid" },
              createdAt: { type: "string", format: "date-time" },
              updatedAt: { type: "string", format: "date-time" },
            },
          },
          ApiToken: {
            type: "object",
            properties: {
              id: { type: "string", format: "uuid" },
              name: { type: "string" },
              tokenPrefix: { type: "string" },
              smtpConfigId: { type: "string", format: "uuid" },
              userId: { type: "string", format: "uuid" },
              expiresAt: { type: ["string", "null"], format: "date-time" },
              lastUsedAt: { type: ["string", "null"], format: "date-time" },
              createdAt: { type: "string", format: "date-time" },
            },
          },
          EmailLog: {
            type: "object",
            properties: {
              id: { type: "string", format: "uuid" },
              smtpConfigId: { type: "string", format: "uuid" },
              userId: { type: ["string", "null"], format: "uuid" },
              from: { type: "string" },
              to: { type: "string" },
              subject: { type: "string" },
              status: { type: "string", enum: ["queued", "sent", "failed"] },
              error: { type: ["string", "null"] },
              createdAt: { type: "string", format: "date-time" },
            },
          },
          Recipient: {
            type: "object",
            properties: {
              email: { type: "string", format: "email" },
              name: { type: "string" },
            },
            required: ["email"],
          },
          SendEmailRequest: {
            type: "object",
            properties: {
              from: { type: "string", description: "Sender address (defaults to SMTP config from address)" },
              to: { type: "array", items: { $ref: "#/components/schemas/Recipient" }, minItems: 1 },
              cc: { type: "array", items: { $ref: "#/components/schemas/Recipient" } },
              bcc: { type: "array", items: { $ref: "#/components/schemas/Recipient" } },
              subject: { type: "string", minLength: 1 },
              html: { type: "string" },
              text: { type: "string" },
            },
            required: ["to", "subject"],
          },
        },
      },
    },
  }),
);

app.get("/scalar", Scalar({ url: "/api/openapi.json" }));

app.on(["POST", "GET"], "/auth/*", (c) => {
  const raw = c.req.raw;
  const authz = raw.headers.get("authorization");
  // The web stores the bearer token URL-encoded (lm_token cookie =
  // encodeURIComponent(token)) and some server-side reads forward it still
  // encoded (%2B, %2F, %3D). The token signature is base64, so an encoded value
  // fails verification -> empty session -> dashboard bounce. Decode it back
  // before Better Auth sees it. (The client sends it already-decoded, which has
  // no "%", so this only touches the encoded case.)
  if (authz && /^Bearer\s+\S*%/.test(authz)) {
    const token = decodeURIComponent(authz.replace(/^Bearer\s+/, ""));
    const headers = new Headers(raw.headers);
    headers.set("authorization", `Bearer ${token}`);
    const req = new Request(raw.url, {
      method: raw.method,
      headers,
      body:
        raw.method === "GET" || raw.method === "HEAD" ? undefined : raw.body,
      // @ts-expect-error duplex is required by undici when a body is streamed
      duplex: "half",
    });
    return auth.handler(req);
  }
  return auth.handler(raw);
});

export default app;
export type ServerType = typeof app;
export { formsPublicApp } from "./forms-public";
