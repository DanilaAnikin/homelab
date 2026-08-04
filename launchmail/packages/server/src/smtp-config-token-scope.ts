import { createMiddleware } from "hono/factory"
import type { AppVariables } from "."

const INCOMING_EMAILS_PATH = "/api/incoming-emails"

/**
 * SMTP-config-bound API tokens are mailbox capabilities, not organization-wide
 * sessions. Keep this allowlist exact so adding a new API route cannot
 * accidentally widen their authority.
 */
export function isSmtpConfigBoundRequestAllowed(
  path: string,
  method: string
): boolean {
  const normalizedMethod = method.toUpperCase()
  return (
    (path === "/api/me" && normalizedMethod === "GET") ||
    (path === "/api/mail/send" && normalizedMethod === "POST") ||
    path === INCOMING_EMAILS_PATH ||
    path.startsWith(`${INCOMING_EMAILS_PATH}/`)
  )
}

export const smtpConfigBoundTokenScopeMiddleware =
  createMiddleware<AppVariables>(async (c, next) => {
    if (!c.get("apiTokenSmtpConfigId")) return next()

    const path = new URL(c.req.url).pathname
    if (!isSmtpConfigBoundRequestAllowed(path, c.req.method)) {
      return c.json(
        { error: "SMTP-config-bound tokens cannot access this resource" },
        403
      )
    }

    return next()
  })
