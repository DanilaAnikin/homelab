import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { WEBHOOK_EVENTS } from "@workspace/db/schemas"

describe("webhook event registry", () => {
  it("contains every typed terminal email outcome", () => {
    expect(WEBHOOK_EVENTS).toEqual(
      expect.arrayContaining([
        "email.sent",
        "email.failed",
        "email.bounced",
        "email.suppressed",
      ])
    )
  })

  it("drives the dashboard from the backend registry instead of a local list", () => {
    const page = readFileSync(
      new URL(
        "../../../apps/web/app/(dashboard)/dashboard/webhooks/page.tsx",
        import.meta.url
      ),
      "utf8"
    )
    const manager = readFileSync(
      new URL(
        "../../../apps/web/app/(dashboard)/dashboard/webhooks/webhooks-manager.tsx",
        import.meta.url
      ),
      "utf8"
    )

    expect(page).toContain(
      'apiGet<{ events: Event[] }>("/api/webhooks/events")'
    )
    expect(manager).toContain("availableEvents.map")
    expect(manager).not.toContain("const EVENTS")
  })
})
