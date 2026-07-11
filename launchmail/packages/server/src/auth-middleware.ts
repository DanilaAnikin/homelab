import { auth } from "@workspace/auth";
import { createMiddleware } from "hono/factory";
import type { AppVariables } from ".";
import { resolveSessionOrg } from "./org-context";

export const authMiddleware = createMiddleware<AppVariables>(async (c, next) => {
  const session = await auth.api.getSession({ headers: c.req.raw.headers });

  if (!session) {
    c.set("user", null);
    c.set("session", null);
    c.set("organizationId", null);
    c.set("role", null);
    c.set("apiTokenSmtpConfigId", null);
    c.set("apiTokenName", null);
    await next();
    return;
  }

  c.set("user", session.user);
  c.set("session", session.session);

  const { organizationId, role } = await resolveSessionOrg(
    session.user.id,
    session.session.activeOrganizationId,
  );
  c.set("organizationId", organizationId);
  c.set("role", role);
  c.set("apiTokenSmtpConfigId", null);
  c.set("apiTokenName", null);
  await next();
});
