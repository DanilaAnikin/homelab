import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createWorkerShutdown } from "./worker-shutdown";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const never = () => new Promise<void>(() => {});

function setup(overrides: {
  mailClose?: () => Promise<void>;
  webhookClose?: () => Promise<void>;
  imapStop?: () => Promise<void>;
} = {}) {
  const order: string[] = [];
  const resources = {
    mailWorker: {
      close: vi.fn(async () => {
        order.push("mail.close");
        await (overrides.mailClose?.() ?? Promise.resolve());
        order.push("mail.closed");
      }),
    },
    webhookWorker: {
      close: vi.fn(async () => {
        order.push("webhook.close");
        await (overrides.webhookClose?.() ?? Promise.resolve());
      }),
    },
    webhookOutboxRelay: {
      stop: vi.fn(async () => {
        order.push("relay.stop");
      }),
    },
    inboxIdle: {
      stop: vi.fn(async () => {
        order.push("imap.stop");
        await (overrides.imapStop?.() ?? Promise.resolve());
      }),
    },
    closeConnections: vi.fn(async () => {
      order.push("redis.close");
    }),
  };
  const exit = vi.fn();
  const log = { log: vi.fn(), error: vi.fn() };
  const shutdown = createWorkerShutdown(resources, {
    deadlineMs: 10_000,
    imapStopMs: 3_000,
    exit,
    log,
  });
  return { order, resources, exit, log, shutdown };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("worker shutdown order", () => {
  it("closes BullMQ at once while IMAP is still logging out, and exits only after the in-flight SMTP job", async () => {
    const smtpJob = deferred();
    const { order, resources, exit, log, shutdown } = setup({
      mailClose: () => smtpJob.promise,
      imapStop: never, // e.g. Gmail not answering LOGOUT
    });

    const done = shutdown("SIGTERM received", 0);
    await vi.advanceTimersByTimeAsync(0);
    // Nothing waits for IMAP before BullMQ is asked to close.
    expect(order).toEqual(["mail.close", "webhook.close", "relay.stop", "imap.stop"]);

    await vi.advanceTimersByTimeAsync(5_000); // IMAP limit (3 s) passed, job still sending
    expect(exit).not.toHaveBeenCalled();
    expect(resources.closeConnections).not.toHaveBeenCalled();

    smtpJob.resolve(); // the job records its result at 5 s
    await done;
    expect(order.slice(-2)).toEqual(["mail.closed", "redis.close"]);
    expect(exit).toHaveBeenCalledTimes(1);
    expect(exit).toHaveBeenCalledWith(0);
    expect(log.error).toHaveBeenCalledWith(
      "[worker] IMAP inbox failed to stop: did not finish within 3 s",
    );
  });

  it("exits at the deadline if a close hangs, and exactly once", async () => {
    const stuck = deferred();
    const { exit, log, shutdown } = setup({ mailClose: () => stuck.promise });

    const done = shutdown("fatal uncaughtException", 1);
    await vi.advanceTimersByTimeAsync(9_999);
    expect(exit).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(exit).toHaveBeenCalledWith(1);
    expect(log.error).toHaveBeenCalledWith(
      "[worker] shutdown still running after 10 s — exiting anyway",
    );

    stuck.resolve(); // (with a real process.exit this never runs)
    await done;
    expect(exit).toHaveBeenCalledTimes(1);
  });

  it("logs a failing close without leaking parameters and still closes the rest", async () => {
    const { order, exit, log, shutdown } = setup({
      webhookClose: () =>
        Promise.reject(
          Object.assign(new Error("Failed query: …\nparams: SECRET"), {
            query: 'update "webhook_outbox" set "queued_at" = $1',
            params: ["SECRET"],
            cause: Object.assign(new Error("connection terminated"), {
              code: "08006",
              severity: "ERROR",
            }),
          }),
        ),
    });

    await shutdown("SIGTERM received", 0);
    expect(order).toContain("redis.close");
    expect(exit).toHaveBeenCalledWith(0);
    const logged = log.error.mock.calls.map((c) => String(c[0])).join("\n");
    expect(logged).toContain("[worker] webhook worker failed to stop: Database error 08006");
    expect(logged).not.toContain("SECRET");
  });

  it("ignores a second trigger (another signal, a fatal error during shutdown)", async () => {
    const { resources, exit, shutdown } = setup();
    await Promise.all([shutdown("SIGTERM received", 0), shutdown("fatal unhandledRejection", 1)]);
    expect(resources.mailWorker.close).toHaveBeenCalledTimes(1);
    expect(exit).toHaveBeenCalledTimes(1);
    expect(exit).toHaveBeenCalledWith(0);
  });
});
