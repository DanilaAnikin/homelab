import { describe, expect, it } from "vitest"
import {
  buildEmailQueueJobOptions,
  createEmailJobId,
  emailLogIdForJob,
} from "./email-job-identity"

class ResettableQueueCounter {
  private counter = 0

  reset(): void {
    this.counter = 0
  }

  accept(options: { jobId?: string }): {
    id: string
    numericSequence: number
  } {
    const numericSequence = ++this.counter
    return {
      id: options.jobId ?? String(numericSequence),
      numericSequence,
    }
  }
}

describe("durable email job identity", () => {
  it("uses a fresh UUID as the BullMQ job id", () => {
    const first = createEmailJobId()
    const second = createEmailJobId()

    expect(first).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    )
    expect(second).not.toBe(first)
    expect(buildEmailQueueJobOptions(undefined, first)).toEqual({
      jobId: first,
    })
  })

  it("cannot collide with persistent logs after Redis history resets", () => {
    const queue = new ResettableQueueCounter()
    const persistentLogIds = new Set<string>()

    // Legacy BullMQ behavior: no custom id, so PostgreSQL keeps a log derived
    // from Redis' low numeric id even after that Redis history disappears.
    const beforeReset = queue.accept({})
    persistentLogIds.add(emailLogIdForJob(beforeReset.id))

    queue.reset()
    const afterReset = queue.accept(
      buildEmailQueueJobOptions(
        undefined,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
      )
    )

    // Redis really did reuse low sequence number 1, while the custom BullMQ id
    // — and therefore the persistent PostgreSQL log id — stayed new.
    expect(beforeReset.id).toBe("1")
    expect(afterReset.numericSequence).toBe(beforeReset.numericSequence)
    expect(afterReset.id).not.toBe(beforeReset.id)
    expect(persistentLogIds.has(emailLogIdForJob(afterReset.id))).toBe(false)

    // Every retry of the accepted job still resolves to exactly one log row.
    expect(emailLogIdForJob(afterReset.id)).toBe(
      emailLogIdForJob(afterReset.id)
    )
  })

  it("preserves scheduled delivery while attaching the durable job id", () => {
    expect(
      buildEmailQueueJobOptions(
        { sendAt: "2026-08-06T10:20:00.000Z" },
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        Date.parse("2026-08-06T10:19:00.000Z")
      )
    ).toEqual({
      jobId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      delay: 60_000,
    })
  })
})
