import { describe, expect, it, vi } from "vitest"
import type { Job } from "bullmq"
import type { EmailJobData } from "./queue"

vi.mock("bullmq", () => {
  class DelayedError extends Error {}
  return {
    DelayedError,
    Queue: class {
      add = vi.fn()
      close = vi.fn()
    },
    Worker: class {
      on = vi.fn()
      close = vi.fn()
    },
  }
})

import {
  emailTerminalIdempotencyKey,
  shouldCommitEmailTerminal,
} from "./email-terminal"
import {
  finalizeSuppressedEmail,
  handleEmailProcessingFailure,
  type FailureHandlingDependencies,
} from "./worker"

const data: EmailJobData = {
  smtpConfigId: "71dfe3e2-b5d6-4736-9d5c-e63411461ac2",
  organizationId: "org-1",
  userId: "user-1",
  from: "Freio <contact@freio.cz>",
  to: [{ email: "school@example.cz" }],
  subject: "Freio pro školy",
  text: "Dobrý den",
  clientReference: "e8b4fe0c-28fe-493a-962a-9886b53f9eed",
  clientType: "freio_b2b_outreach",
}

function job(attemptsStarted: number, attempts = 3): Job<EmailJobData> {
  return {
    id: "mail-job-1",
    data,
    attemptsStarted,
    opts: { attempts },
  } as Job<EmailJobData>
}

function dependencies(): FailureHandlingDependencies {
  return {
    finalize: vi.fn().mockResolvedValue(undefined),
    recordDeferred: vi.fn().mockResolvedValue(undefined),
    suppress: vi.fn().mockResolvedValue(undefined),
  }
}

describe("accepted mail terminal finalization", () => {
  it("emits email.suppressed with complete correlation and no SMTP send", async () => {
    const finalize = vi.fn().mockResolvedValue(undefined)

    await finalizeSuppressedEmail(job(1), "log-id-1", finalize)

    expect(finalize).toHaveBeenCalledWith(
      expect.objectContaining({
        trackingId: "log-id-1",
        jobId: "mail-job-1",
        status: "suppressed",
        event: "email.suppressed",
        data: {
          jobId: "mail-job-1",
          logId: "log-id-1",
          smtpConfigId: data.smtpConfigId,
          to: ["school@example.cz"],
          reason: "all_recipients_suppressed",
          clientReference: data.clientReference,
          clientType: data.clientType,
        },
      })
    )
  })

  it("turns a final pre-SMTP exception into one typed failed terminal", async () => {
    const deps = dependencies()
    const missingConfig = new Error(
      `SMTP config ${data.smtpConfigId} not found`
    )

    await expect(
      handleEmailProcessingFailure(
        job(1),
        "log-id-1",
        data.to,
        missingConfig,
        deps
      )
    ).resolves.toEqual({ status: "failed", willRetry: false })

    expect(deps.recordDeferred).not.toHaveBeenCalled()
    expect(deps.finalize).toHaveBeenCalledOnce()
    expect(deps.finalize).toHaveBeenCalledWith(
      expect.objectContaining({
        trackingId: "log-id-1",
        event: "email.failed",
        status: "failed",
        error: missingConfig.message,
      })
    )
  })

  it("does not emit a terminal event before the last retryable attempt", async () => {
    const deps = dependencies()

    await expect(
      handleEmailProcessingFailure(
        job(1),
        "log-id-1",
        data.to,
        new Error("temporary database error"),
        deps
      )
    ).resolves.toEqual({ status: "deferred", willRetry: true })

    expect(deps.recordDeferred).toHaveBeenCalledOnce()
    expect(deps.finalize).not.toHaveBeenCalled()
  })

  it("uses one stable finalization key for duplicate invocations", () => {
    expect(emailTerminalIdempotencyKey("log-id-1", "email.failed")).toBe(
      emailTerminalIdempotencyKey("log-id-1", "email.failed")
    )
    expect(emailTerminalIdempotencyKey("log-id-1", "email.failed")).not.toBe(
      emailTerminalIdempotencyKey("log-id-2", "email.failed")
    )
    expect(shouldCommitEmailTerminal("failed", "failed")).toBe(false)
    expect(shouldCommitEmailTerminal("failed", "bounced")).toBe(false)
    expect(shouldCommitEmailTerminal("sent", "bounced")).toBe(true)
  })
})
