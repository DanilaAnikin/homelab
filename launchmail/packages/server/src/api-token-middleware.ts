import { createMiddleware } from "hono/factory";
import {
  findApiTokenByHash,
  hashToken,
  touchApiToken,
} from "@workspace/mail-queue";
import type { AppVariables } from ".";

export const apiTokenMiddleware = createMiddleware<AppVariables>(
  async (c, next) => {
    const authHeader = c.req.header("Authorization");
    if (!authHeader?.startsWith("Bearer ")) {
      await next();
      return;
    }

    // Only LaunchMail API keys start with "lm_". Anything else (e.g. a Better
    // Auth session bearer token) is left for the session middleware/handler.
    const token = authHeader.slice(7);
    if (!token.startsWith("lm_")) {
      await next();
      return;
    }

    const apiToken = await findApiTokenByHash(hashToken(token));
    if (!apiToken) {
      return c.json({ error: "Invalid API token" }, 401);
    }
    if (apiToken.expiresAt && new Date(apiToken.expiresAt) < new Date()) {
      return c.json({ error: "API token has expired" }, 401);
    }

    await touchApiToken(apiToken.id);

    // The request authenticated with an API token: it owns the request context.
    // Clear any session the auth middleware may have resolved from a cookie so
    // audit attribution is unambiguously the token (never a mix of both).
    c.set("user", null);
    c.set("session", null);
    c.set("organizationId", apiToken.organizationId);
    c.set("role", apiToken.role);
    c.set("apiTokenSmtpConfigId", apiToken.smtpConfigId);
    c.set("apiTokenName", apiToken.name);
    await next();
  },
);
