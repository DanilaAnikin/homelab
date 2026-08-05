import { beforeEach, describe, expect, it, vi } from "vitest"

const queueMocks = vi.hoisted(() => ({
  add: vi.fn(),
}))

vi.mock("bullmq", () => ({
  Queue: class {
    add = queueMocks.add
  },
}))
vi.mock("./redis", () => ({ REDIS_URL: "redis://queue-test" }))

import { enqueueEmail, type EmailJobData } from "./queue"

const data: EmailJobData = {
  smtpConfigId: "71dfe3e2-b5d6-4736-9d5c-e63411461ac2",
  organizationId: "org-1",
  from: "Freio <contact@freio.cz>",
  to: [{ email: "school@example.cz" }],
  subject: "Freio pro školy",
  text: "Dobrý den",
  clientReference: "e8b4fe0c-28fe-493a-962a-9886b53f9eed",
  clientType: "freio_b2b_outreach",
}

describe("enqueueEmail", () => {
  beforeEach(() => {
    queueMocks.add.mockReset()
    queueMocks.add.mockImplementation(
      async (
        _name: string,
        _data: EmailJobData,
        options: { jobId: string }
      ) => ({ id: options.jobId })
    )
  })

  it("passes a globally unique custom id to BullMQ without changing caller correlation", async () => {
    const first = await enqueueEmail(data)
    const second = await enqueueEmail(data)

    expect(first.id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    )
    expect(second.id).not.toBe(first.id)
    expect(queueMocks.add).toHaveBeenNthCalledWith(1, "send-email", data, {
      jobId: first.id,
    })
    expect(data.clientReference).toBe("e8b4fe0c-28fe-493a-962a-9886b53f9eed")
  })
})
