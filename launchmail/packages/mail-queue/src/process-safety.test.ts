import { describe, expect, it, vi } from "vitest";
import {
  handleProcessFailure,
  isImapConnectionError,
  type ProcessSafetyNetOptions,
} from "./process-safety";

function options(onFatal = vi.fn()) {
  const log = { error: vi.fn() };
  const opts: ProcessSafetyNetOptions = { name: "worker", onFatal, log };
  return { opts, log, onFatal };
}

describe("process safety net", () => {
  it("recognises errors emitted by an ImapFlow connection", () => {
    expect(
      isImapConnectionError(
        Object.assign(new Error("Socket timeout"), { code: "ETIMEOUT", _connId: "abc" }),
      ),
    ).toBe(true);
    expect(isImapConnectionError(new Error("TypeError somewhere"))).toBe(false);
    expect(isImapConnectionError("string reason")).toBe(false);
  });

  it("logs and survives an isolated IMAP connection error", () => {
    const { opts, log, onFatal } = options();
    const error = Object.assign(new Error("Connection not available"), {
      code: "NoConnection",
      _connId: "zqcx",
    });

    expect(handleProcessFailure("uncaughtException", error, opts, { fatal: false })).toBe(
      "continue",
    );
    expect(onFatal).not.toHaveBeenCalled();
    expect(log.error).toHaveBeenCalledWith(
      expect.stringContaining("IMAP connection error without a listener"),
    );
  });

  it("treats any other error as fatal, logs it with frames and shuts down exactly once", async () => {
    const { opts, log, onFatal } = options();
    const state = { fatal: false };

    expect(handleProcessFailure("unhandledRejection", new TypeError("boom"), opts, state)).toBe(
      "fatal",
    );
    expect(handleProcessFailure("uncaughtException", new Error("again"), opts, state)).toBe(
      "fatal",
    );
    await Promise.resolve();
    await Promise.resolve();

    expect(onFatal).toHaveBeenCalledTimes(1);
    expect(onFatal).toHaveBeenCalledWith("unhandledRejection", expect.any(TypeError));
    expect(log.error).toHaveBeenCalledWith(
      expect.stringMatching(/^\[worker\] FATAL unhandledRejection: boom\n\s+at /),
    );
  });

  it("does not leak drizzle params into the fatal log", () => {
    const { opts, log } = options();
    const error = Object.assign(new Error("Failed query: insert …\nparams: SECRET"), {
      query: 'insert into "incoming_emails" ("id") values ($1)',
      params: ["SECRET"],
      cause: Object.assign(new Error("connection terminated"), {
        code: "08006",
        severity: "FATAL",
      }),
    });
    handleProcessFailure("unhandledRejection", error, opts, { fatal: true });
    const logged = String(log.error.mock.calls[0]?.[0]);
    expect(logged).toContain("Database error 08006: connection terminated");
    expect(logged).not.toContain("SECRET");
  });
});
