import { describe, expect, it, vi } from "vitest";
import { ImapFlow } from "imapflow";
import { createImapClient } from "./imap-client";

// Uses the REAL imapflow EventEmitter (no network: connect() is never called).
const options = {
  host: "imap.example.test",
  port: 993,
  secure: true,
  auth: { user: "user@example.test", pass: "not-used" },
  logger: false as const,
};

describe("createImapClient", () => {
  it("a bare ImapFlow turns an 'error' event into a throw (what crashed the worker)", () => {
    const bare = new ImapFlow(options);
    expect(() => bare.emit("error", new Error("Socket timeout"))).toThrow("Socket timeout");
  });

  it("handles 'error' events: logs a content-free line and notifies the owner", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const onError = vi.fn();
    const client = createImapClient(options, "Osobní Gmail", onError);
    const error = Object.assign(new Error("Socket timeout"), {
      code: "ETIMEOUT",
      _connId: "x5nawl0lfwib9gipb7cd",
    });

    expect(() => client.emit("error", error)).not.toThrow();
    expect(onError).toHaveBeenCalledWith(error);
    expect(warn).toHaveBeenCalledWith(
      "[imap] Osobní Gmail: connection error: Socket timeout [ETIMEOUT]",
    );
    warn.mockRestore();
  });

  it("a throwing owner callback cannot escape the listener", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    const client = createImapClient(options, "Mailbox", () => {
      throw new Error("bug in owner");
    });
    expect(() => client.emit("error", new Error("Connection not available"))).not.toThrow();
    expect(err).toHaveBeenCalledWith(expect.stringContaining("bug in owner"));
    warn.mockRestore();
    err.mockRestore();
  });
});
