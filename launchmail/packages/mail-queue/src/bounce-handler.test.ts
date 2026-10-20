import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  selects: 0,
  rows: [] as unknown[],
  dispatchEvent: vi.fn(),
  addSuppression: vi.fn(),
}));

vi.mock("@workspace/db", () => ({
  db: {
    select: () => {
      m.selects += 1;
      const chain = {
        from: () => chain,
        where: () => chain,
        orderBy: () => chain,
        limit: async () => m.rows,
      };
      return chain;
    },
    transaction: vi.fn(),
  },
}));
vi.mock("./webhooks.service", () => ({ dispatchEvent: m.dispatchEvent }));
vi.mock("./suppressions.service", () => ({ addSuppression: m.addSuppression }));
vi.mock("./webhook-outbox", () => ({
  persistWebhookEvent: vi.fn(),
  relayWebhookOutboxRows: vi.fn(),
}));

import { processBounce } from "./bounce-handler";

beforeEach(() => {
  m.selects = 0;
  m.rows = [];
  m.dispatchEvent.mockReset().mockResolvedValue(undefined);
  m.addSuppression.mockReset().mockResolvedValue(undefined);
});

describe("processBounce VERP attribution", () => {
  it("never compares email_logs.id (uuid) with a token that is not a UUID", async () => {
    // e.g. someone else's VERP address: bounces+log-abc-123@… — querying a
    // uuid column with it is a 22P02 error that aborted the whole bounce.
    await processBounce("org-1", {
      isBounce: true,
      permanent: true,
      verpToken: "log-abc-123",
    });
    expect(m.selects).toBe(0);
    expect(m.dispatchEvent).toHaveBeenCalledWith(
      "org-1",
      "email.bounced",
      expect.objectContaining({ logId: null }),
    );
  });

  it("looks a UUID token up and falls back to the recipient when it is unknown", async () => {
    await processBounce("org-1", {
      isBounce: true,
      permanent: true,
      recipient: "dead@example.test",
      verpToken: "0f7b2d7e-5a43-5b8b-9f0c-3a2f6e0d9c11",
    });
    expect(m.selects).toBe(2); // VERP lookup, then newest sent log to the recipient
    expect(m.addSuppression).toHaveBeenCalledWith("org-1", "dead@example.test", "bounce");
  });
});
