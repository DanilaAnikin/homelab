import { describe, expect, it, vi } from "vitest"
import type { FinalizeEmailTerminalResult } from "./email-terminal"
import { assertExpectedEmailTerminalOutboxRows } from "./email-terminal-outbox-contract"
import {
  buildIncident20260805FinalizeInput,
  buildIncident20260805RecoveryPlan,
  executeIncident20260805Recovery,
  incident20260805Sha256,
  incident20260805TrackingId,
  type Incident20260805ExistingLog,
  type Incident20260805JobId,
  type Incident20260805JobSnapshot,
  validateIncident20260805Webhook,
} from "./incident-2026-08-05-recovery"

const ORGANIZATION_ID = "test-freio-organization"
const SMTP_CONFIG_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
const CLIENT_REFERENCES = {
  "1": "10000000-0000-4000-8000-000000000001",
  "2": "20000000-0000-4000-8000-000000000002",
  "3": "30000000-0000-4000-8000-000000000003",
  "4": "40000000-0000-4000-8000-000000000004",
  "5": "50000000-0000-4000-8000-000000000005",
} as const

const TEST_EXPECTED_HASHES = Object.fromEntries(
  Object.entries(CLIENT_REFERENCES).map(([id, reference]) => [
    id,
    incident20260805Sha256(reference),
  ])
) as Record<Incident20260805JobId, string>

function snapshots(): Incident20260805JobSnapshot[] {
  return Object.entries(CLIENT_REFERENCES).map(
    ([id, clientReference], index) => ({
      id,
      state: "completed",
      data: {
        smtpConfigId: SMTP_CONFIG_ID,
        organizationId: ORGANIZATION_ID,
        userId: null,
        from: "Freio Contact <contact@example.test>",
        to: [{ email: `school-${id}@example.test` }],
        subject: `Incident message ${id}`,
        html: `<p>Message ${id}</p>`,
        text: `Message ${id}`,
        clientReference,
        clientType: "freio_b2b_outreach",
      },
      returnvalue:
        id === "1"
          ? { messageId: "<smtp-accepted@example.test>" }
          : { messageId: `<historical-${id}@example.test>`, deduped: true },
      finishedOn: Date.parse(`2026-08-05T08:20:0${index + 1}.000Z`),
    })
  )
}

function recoveredLogs(): Incident20260805ExistingLog[] {
  const initial = recoveryPlan()
  return initial.items.map((item) => ({
    id: item.trackingId,
    clientReference: item.clientReference,
    clientType: item.job.clientType ?? null,
    organizationId: item.job.organizationId ?? null,
    smtpConfigId: item.job.smtpConfigId,
    status: item.outcome,
    providerMessageId: item.providerMessageId,
  }))
}

function recoveryPlan(
  jobs: Incident20260805JobSnapshot[] = snapshots(),
  logs: Incident20260805ExistingLog[] = []
) {
  return buildIncident20260805RecoveryPlan(jobs, logs, {
    expectedClientReferenceSha256: TEST_EXPECTED_HASHES,
  })
}

function committedTerminal(): FinalizeEmailTerminalResult {
  return {
    deduplicated: false,
    outboxRows: [
      {
        id: "90000000-0000-4000-8000-000000000001",
        hookId: "90000000-0000-4000-8000-000000000002",
        organizationId: ORGANIZATION_ID,
        event: "email.sent",
        data: {},
        occurredAt: new Date("2026-08-05T08:20:01.000Z"),
      },
    ],
  }
}

describe("2026-08-05 Freio incident recovery plan", () => {
  it("accepts only the exact five completed jobs and builds one sent plus four failed terminals", () => {
    const plan = recoveryPlan()

    expect(plan.organizationId).toBe(ORGANIZATION_ID)
    expect(plan.smtpConfigId).toBe(SMTP_CONFIG_ID)
    expect(plan.items).toHaveLength(5)
    expect(plan.items.map((item) => item.outcome)).toEqual([
      "sent",
      "failed",
      "failed",
      "failed",
      "failed",
    ])
    expect(plan.items.every((item) => !item.alreadyRecovered)).toBe(true)

    const sent = buildIncident20260805FinalizeInput(plan.items[0]!)
    expect(sent).toMatchObject({
      jobId: "1",
      status: "sent",
      event: "email.sent",
      providerMessageId: "<smtp-accepted@example.test>",
      expectedOutboxRows: 1,
      data: {
        jobId: "1",
        messageId: "<smtp-accepted@example.test>",
        clientType: "freio_b2b_outreach",
      },
    })
    expect(sent.occurredAt).toBeInstanceOf(Date)
    expect(sent.occurredAt!.toISOString()).toBe("2026-08-05T08:20:01.000Z")

    const failed = buildIncident20260805FinalizeInput(plan.items[1]!)
    expect(failed).toMatchObject({
      jobId: "2",
      status: "failed",
      event: "email.failed",
      expectedOutboxRows: 1,
      data: {
        jobId: "2",
        clientType: "freio_b2b_outreach",
      },
    })
    expect(failed.providerMessageId).toBeUndefined()
    expect(failed.error).toContain("SMTP was not attempted")
  })

  it("rejects an unexpected job-to-clientReference mapping", () => {
    const jobs = snapshots()
    jobs[1] = {
      ...jobs[1]!,
      data: {
        ...(jobs[1]!.data as Record<string, unknown>),
        clientReference: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      },
    }

    expect(() => recoveryPlan(jobs)).toThrow("unexpected clientReference")
  })

  it("rejects a deduped job 1 and a non-deduped job 2", () => {
    const wrongSent = snapshots()
    wrongSent[0] = {
      ...wrongSent[0]!,
      returnvalue: {
        messageId: "<smtp-accepted@example.test>",
        deduped: true,
      },
    }
    expect(() => recoveryPlan(wrongSent)).toThrow(
      "not the proven SMTP-accepted job"
    )

    const wrongFailed = snapshots()
    wrongFailed[1] = {
      ...wrongFailed[1]!,
      returnvalue: { messageId: "<new-send@example.test>" },
    }
    expect(() => recoveryPlan(wrongFailed)).toThrow(
      "not a proven false-deduplicated job"
    )
  })

  it("rejects missing queue and SMTP result identifiers", () => {
    const missingJobId = snapshots()
    missingJobId[0] = { ...missingJobId[0]!, id: null }
    expect(() => recoveryPlan(missingJobId)).toThrow(
      "missing or unexpected queue job id"
    )

    const missingMessageId = snapshots()
    missingMessageId[0] = { ...missingMessageId[0]!, returnvalue: {} }
    expect(() => recoveryPlan(missingMessageId)).toThrow(
      "not the proven SMTP-accepted job"
    )
  })

  it("derives a stable recovery id without exposing the reference in it", () => {
    const reference = CLIENT_REFERENCES["1"]
    const first = incident20260805TrackingId(reference)
    const second = incident20260805TrackingId(reference)

    expect(first).toBe(second)
    expect(first).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    )
    expect(first).not.toContain(reference)
    expect(incident20260805Sha256(reference)).toHaveLength(64)
  })

  it("accepts only exact prior recovery rows and reports a rerun as deduplicated", async () => {
    const plan = recoveryPlan(snapshots(), recoveredLogs())
    expect(plan.items.every((item) => item.alreadyRecovered)).toBe(true)

    const finalize = vi.fn(
      async (): Promise<FinalizeEmailTerminalResult> => ({
        deduplicated: true,
        outboxRows: [],
      })
    )
    const results = await executeIncident20260805Recovery(plan, finalize)

    expect(finalize).not.toHaveBeenCalled()
    expect(results.every((result) => result.deduplicated)).toBe(true)
    expect(results.every((result) => result.outboxRows === 0)).toBe(true)
    expect(results.every((result) => result.skipped)).toBe(true)
  })

  it("skips recovered checkpoints and applies only the remaining suffix", async () => {
    const plan = recoveryPlan(snapshots(), recoveredLogs().slice(0, 2))
    const finalize = vi.fn(async () => committedTerminal())

    const results = await executeIncident20260805Recovery(plan, finalize)

    expect(finalize).toHaveBeenCalledTimes(3)
    expect(results.slice(0, 2).every((result) => result.skipped)).toBe(true)
    expect(results.slice(2).every((result) => !result.skipped)).toBe(true)
  })

  it("stops immediately when a pending terminal checkpoint is unexpected", async () => {
    const finalize = vi
      .fn()
      .mockResolvedValueOnce(committedTerminal())
      .mockResolvedValueOnce({ deduplicated: true, outboxRows: [] })

    await expect(
      executeIncident20260805Recovery(recoveryPlan(), finalize)
    ).rejects.toThrow("unexpected terminal checkpoint")
    expect(finalize).toHaveBeenCalledTimes(2)
  })

  it("requires exactly one enabled hook subscribed to both terminal events", () => {
    const valid = {
      id: "90000000-0000-4000-8000-000000000002",
      enabled: true,
      events: ["email.sent", "email.failed", "email.bounced"],
    }
    expect(validateIncident20260805Webhook([valid])).toBe(valid.id)
    expect(() => validateIncident20260805Webhook([])).toThrow(
      "exactly one enabled"
    )
    expect(() => validateIncident20260805Webhook([valid, valid])).toThrow(
      "exactly one enabled"
    )
    expect(() =>
      validateIncident20260805Webhook([{ ...valid, events: ["email.sent"] }])
    ).toThrow("email.sent and email.failed")
  })

  it("enforces the exact outbox count contract used inside terminal transactions", () => {
    expect(() =>
      assertExpectedEmailTerminalOutboxRows(undefined, 0)
    ).not.toThrow()
    expect(() => assertExpectedEmailTerminalOutboxRows(1, 1)).not.toThrow()
    expect(() => assertExpectedEmailTerminalOutboxRows(1, 0)).toThrow(
      "expected 1, created 0"
    )
    expect(() => assertExpectedEmailTerminalOutboxRows(1, 2)).toThrow(
      "expected 1, created 2"
    )
  })

  it("rejects a conflicting durable log instead of overwriting it", () => {
    const logs = recoveredLogs()
    logs[0] = { ...logs[0]!, status: "bounced" }

    expect(() => recoveryPlan(snapshots(), logs)).toThrow(
      "conflicting durable email state"
    )
  })

  it("rejects a deterministic tracking id occupied by an unrelated log", () => {
    const plan = recoveryPlan()
    const collision: Incident20260805ExistingLog = {
      id: plan.items[0]!.trackingId,
      clientReference: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      clientType: "unrelated",
      organizationId: "another-organization",
      smtpConfigId: SMTP_CONFIG_ID,
      status: "sent",
      providerMessageId: null,
    }

    expect(() => recoveryPlan(snapshots(), [collision])).toThrow(
      "unrelated durable email log"
    )
  })
})
