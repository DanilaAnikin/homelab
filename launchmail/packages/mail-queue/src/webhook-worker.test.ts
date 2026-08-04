import { createHmac } from "node:crypto"
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest"

const bull = vi.hoisted(() => ({
  add: vi.fn(),
  closeQueue: vi.fn(),
  on: vi.fn(),
  queueOptions: undefined as unknown,
}))

vi.mock("bullmq", () => ({
  Queue: class {
    constructor(_name: string, options: unknown) {
      bull.queueOptions = options
    }
    add = bull.add
    close = bull.closeQueue
  },
  Worker: class {
    on = bull.on
    close = vi.fn()
  },
}))

import {
  WEBHOOK_JOB_ATTEMPTS,
  closeWebhookQueue,
  enqueueWebhook,
  type WebhookJobData,
} from "./webhook-queue"
import {
  processWebhookDelivery,
  type WebhookDeliveryDependencies,
} from "./webhook-worker"

const job: WebhookJobData = {
  outboxId: "8f4149f1-5301-468d-b2d6-2f96fe033855",
  hookId: "56c84787-d552-443c-b4a2-79be0c52e09a",
  organizationId: "org-1",
  event: "email.sent",
  data: { messageId: "msg-1" },
  occurredAt: "2026-08-04T16:55:00.000Z",
}

const hook = {
  id: job.hookId,
  organizationId: job.organizationId,
  url: "https://receiver.example/webhooks/launchmail",
  events: ["email.sent" as const],
  secret: "whsec_test-secret",
  enabled: true,
}

function dependencies(
  overrides: Partial<WebhookDeliveryDependencies> = {}
): WebhookDeliveryDependencies {
  return {
    loadHook: vi.fn().mockResolvedValue(hook),
    post: vi.fn().mockResolvedValue({ status: 204 }),
    recordAttempt: vi.fn().mockResolvedValue(undefined),
    complete: vi.fn().mockResolvedValue(undefined),
    now: () => new Date("2026-08-04T17:00:00.000Z"),
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterAll(async () => {
  await closeWebhookQueue()
})

describe("durable webhook queue", () => {
  it("stores only delivery identifiers/event data and configures eight retries", async () => {
    bull.add.mockResolvedValue({ id: "job-1" })

    await enqueueWebhook(job, { jobId: `outbox-${job.outboxId}` })

    expect(bull.add).toHaveBeenCalledWith("deliver-webhook", job, {
      jobId: `outbox-${job.outboxId}`,
    })
    expect(Object.keys(bull.add.mock.calls[0]![1]).sort()).toEqual([
      "data",
      "event",
      "hookId",
      "occurredAt",
      "organizationId",
      "outboxId",
    ])
    expect(WEBHOOK_JOB_ATTEMPTS).toBe(8)
    expect(bull.queueOptions).toMatchObject({
      defaultJobOptions: {
        attempts: 8,
        backoff: { type: "exponential", delay: 30_000 },
      },
    })
  })

  it("exports a graceful producer close", async () => {
    bull.closeQueue.mockResolvedValue(undefined)
    await closeWebhookQueue()
    expect(bull.closeQueue).toHaveBeenCalledOnce()
  })
})

describe("processWebhookDelivery", () => {
  it("delivers an enabled matching hook with a fresh database secret", async () => {
    const deps = dependencies()

    await expect(processWebhookDelivery(job, deps)).resolves.toEqual({
      delivered: true,
      status: 204,
    })

    const body = JSON.stringify({
      event: "email.sent",
      createdAt: job.occurredAt,
      data: job.data,
    })
    const signature = createHmac("sha256", hook.secret)
      .update(body)
      .digest("hex")
    expect(deps.post).toHaveBeenCalledWith(
      hook.url,
      body,
      {
        "Content-Type": "application/json",
        "X-LaunchMail-Event": "email.sent",
        "X-LaunchMail-Signature": `sha256=${signature}`,
      },
      10_000
    )
    expect(deps.recordAttempt).toHaveBeenCalledWith(
      hook.id,
      hook.organizationId,
      "204",
      new Date("2026-08-04T17:00:00.000Z")
    )
    expect(deps.complete).toHaveBeenCalledWith(job.outboxId)
  })

  it.each([
    ["deleted", null],
    ["disabled", { ...hook, enabled: false }],
    ["event-disabled", { ...hook, events: ["email.failed" as const] }],
  ])("skips a %s hook without making a request", async (reason, loadedHook) => {
    const deps = dependencies({
      loadHook: vi.fn().mockResolvedValue(loadedHook),
    })

    await expect(processWebhookDelivery(job, deps)).resolves.toEqual({
      delivered: false,
      skipped: reason,
    })
    expect(deps.post).not.toHaveBeenCalled()
    expect(deps.recordAttempt).not.toHaveBeenCalled()
    expect(deps.complete).toHaveBeenCalledWith(job.outboxId)
  })

  it("throws on a non-2xx response so BullMQ retries the job", async () => {
    const deps = dependencies({
      post: vi.fn().mockResolvedValue({ status: 503 }),
    })

    await expect(processWebhookDelivery(job, deps)).rejects.toThrow(
      "responded with HTTP 503"
    )
    expect(deps.recordAttempt).toHaveBeenCalledWith(
      hook.id,
      hook.organizationId,
      "503",
      expect.any(Date)
    )
    expect(deps.complete).not.toHaveBeenCalled()
  })

  it("throws on network errors so BullMQ retries the job", async () => {
    const networkError = new Error("ECONNRESET")
    const deps = dependencies({
      post: vi.fn().mockRejectedValue(networkError),
    })

    await expect(processWebhookDelivery(job, deps)).rejects.toBe(networkError)
    expect(deps.recordAttempt).toHaveBeenCalledWith(
      hook.id,
      hook.organizationId,
      "error",
      expect.any(Date)
    )
    expect(deps.complete).not.toHaveBeenCalled()
  })

  it("keeps the original occurredAt across retry attempts", async () => {
    const first = dependencies({
      post: vi.fn().mockResolvedValue({ status: 503 }),
      now: () => new Date("2026-08-04T17:00:00.000Z"),
    })
    const second = dependencies({
      now: () => new Date("2026-08-04T18:00:00.000Z"),
    })

    await expect(processWebhookDelivery(job, first)).rejects.toThrow()
    await processWebhookDelivery(job, second)

    const firstBody = vi.mocked(first.post).mock.calls[0]![1]
    const secondBody = vi.mocked(second.post).mock.calls[0]![1]
    expect(JSON.parse(firstBody).createdAt).toBe(job.occurredAt)
    expect(JSON.parse(secondBody).createdAt).toBe(job.occurredAt)
  })

  it("delivers email.suppressed through the same retryable queue", async () => {
    const suppressedJob: WebhookJobData = {
      ...job,
      event: "email.suppressed",
      data: {
        messageId: "msg-1",
        reason: "all_recipients_suppressed",
      },
    }
    const deps = dependencies({
      loadHook: vi.fn().mockResolvedValue({
        ...hook,
        events: ["email.suppressed"],
      }),
    })

    await expect(processWebhookDelivery(suppressedJob, deps)).resolves.toEqual({
      delivered: true,
      status: 204,
    })
    expect(deps.post).toHaveBeenCalledWith(
      hook.url,
      expect.stringContaining('"event":"email.suppressed"'),
      expect.objectContaining({
        "X-LaunchMail-Event": "email.suppressed",
      }),
      10_000
    )
  })
})
