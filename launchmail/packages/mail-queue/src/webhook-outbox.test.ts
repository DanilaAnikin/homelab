import { describe, expect, it, vi } from "vitest"

vi.mock("bullmq", () => ({
  Queue: class {
    add = vi.fn()
    close = vi.fn()
  },
}))
import {
  buildWebhookOutboxJob,
  shouldRelayWebhookOutboxRow,
} from "./webhook-outbox"

const now = new Date("2026-08-05T18:00:00.000Z")
const pending = {
  queuedAt: null,
  completedAt: null,
  failedAt: null,
}

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

  it("selects never-queued and stale queued rows for relay", () => {
    expect(shouldRelayWebhookOutboxRow(pending, now)).toBe(true)
    expect(
      shouldRelayWebhookOutboxRow(
        {
          ...pending,
          queuedAt: new Date(now.getTime() - 5 * 60 * 1000 - 1),
        },
        now
      )
    ).toBe(true)
  })

  it("excludes fresh queued and terminal rows", () => {
    expect(
      shouldRelayWebhookOutboxRow(
        {
          ...pending,
          queuedAt: new Date(now.getTime() - 5 * 60 * 1000),
        },
        now
      )
    ).toBe(false)
    expect(
      shouldRelayWebhookOutboxRow(
        { ...pending, completedAt: new Date(now.getTime() - 1) },
        now
      )
    ).toBe(false)
    expect(
      shouldRelayWebhookOutboxRow(
        { ...pending, failedAt: new Date(now.getTime() - 1) },
        now
      )
    ).toBe(false)
  })

  it("reuses one deterministic queue identity when a stale row is relayed", () => {
    const row = {
      id: "6cdd1600-26aa-4701-b7dc-74b52271a3e8",
      hookId: "b57bdc49-338d-4a44-b22f-c2743a420d52",
      organizationId: "org-1",
      event: "email.suppressed" as const,
      data: { reason: "all_recipients_suppressed" },
      occurredAt: new Date("2026-08-04T16:55:00.000Z"),
    }
    const existing = buildWebhookOutboxJob(row)
    const replay = buildWebhookOutboxJob(row)
    const queue = new Map<string, unknown>()

    queue.set(existing.options.jobId, existing.data)
    queue.set(replay.options.jobId, replay.data)

    expect(replay.options.jobId).toBe(existing.options.jobId)
    expect(queue.size).toBe(1)
  })
})
