import { Hono } from "hono"
import { describe, expect, it } from "vitest"
import type { AppVariables } from "."
import {
  isSmtpConfigBoundRequestAllowed,
  smtpConfigBoundTokenScopeMiddleware,
} from "./smtp-config-token-scope"

const BOUND_CONFIG_ID = "88d218c0-a39a-419b-9b44-2688967f971a"

function scopedApp(boundConfigId: string | null) {
  return new Hono<AppVariables>()
    .use("*", async (c, next) => {
      c.set("apiTokenSmtpConfigId", boundConfigId)
      await next()
    })
    .use("*", smtpConfigBoundTokenScopeMiddleware)
    .all("*", (c) => c.json({ reached: true }))
}

describe("isSmtpConfigBoundRequestAllowed", () => {
  it.each([
    ["GET", "/api/me"],
    ["POST", "/api/mail/send"],
    ["GET", "/api/incoming-emails"],
    ["POST", "/api/incoming-emails/sync"],
    ["DELETE", "/api/incoming-emails/message-id"],
  ])("allows %s %s", (method, path) => {
    expect(isSmtpConfigBoundRequestAllowed(path, method)).toBe(true)
  })

  it.each([
    ["POST", "/api/me"],
    ["GET", "/api/mail/send"],
    ["POST", "/api/mail"],
    ["GET", "/api/smtp-configs"],
    ["POST", "/api/api-keys"],
    ["GET", "/api/domains"],
    ["GET", "/api/webhooks"],
    ["GET", "/api/logs"],
    ["GET", "/api/incoming-emails-elsewhere"],
  ])("rejects %s %s", (method, path) => {
    expect(isSmtpConfigBoundRequestAllowed(path, method)).toBe(false)
  })
})

describe("smtpConfigBoundTokenScopeMiddleware", () => {
  it("returns 403 before a disallowed route is reached", async () => {
    const response = await scopedApp(BOUND_CONFIG_ID).request(
      "/api/smtp-configs",
      { method: "GET" }
    )

    expect(response.status).toBe(403)
    expect(await response.json()).toEqual({
      error: "SMTP-config-bound tokens cannot access this resource",
    })
  })

  it("lets an allowed mailbox route continue", async () => {
    const response = await scopedApp(BOUND_CONFIG_ID).request(
      "/api/incoming-emails/message-id/read",
      { method: "POST" }
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ reached: true })
  })

  it("does not constrain an unbound token/session", async () => {
    const response = await scopedApp(null).request("/api/domains", {
      method: "DELETE",
    })

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ reached: true })
  })
})
