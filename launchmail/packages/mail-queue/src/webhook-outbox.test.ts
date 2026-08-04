import { describe, expect, it, vi } from "vitest"

vi.mock("bullmq", () => ({
  Queue: class {
    add = vi.fn()
    close = vi.fn()
  },
}))
import { buildWebhookOutboxJob } from "./webhook-outbox"

describe("webhook outbox relay contract", () => {
  it("builds a secret-free, deterministic job with immutable occurrence time", () => {
    const job = buildWebhookOutboxJob({
      id: "6cdd1600-26aa-4701-b7dc-74b52271a3e8",
      hookId: "b57bdc49-338d-4a44-b22f-c2743a420d52",
      organizationId: "org-1",
      event: "email.suppressed",
      data: { reason: "all_recipients_suppressed" },
      occurredAt: new Date("2026-08-04T16:55:00.000Z"),
    })

    expect(job).toEqual({
      data: {
        outboxId: "6cdd1600-26aa-4701-b7dc-74b52271a3e8",
        hookId: "b57bdc49-338d-4a44-b22f-c2743a420d52",
        organizationId: "org-1",
        event: "email.suppressed",
        data: { reason: "all_recipients_suppressed" },
        occurredAt: "2026-08-04T16:55:00.000Z",
      },
      options: { jobId: "outbox-6cdd1600-26aa-4701-b7dc-74b52271a3e8" },
    })
    expect(JSON.stringify(job)).not.toContain("whsec_")
    expect(Object.keys(job.data)).not.toContain("secret")
  })
})
