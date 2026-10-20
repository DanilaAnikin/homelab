import { describe, expect, it } from "vitest";
import { describeError, stackFrames } from "./safe-error";

describe("describeError", () => {
  it("never includes drizzle query params (they carry whole e-mails)", () => {
    const cause = Object.assign(
      new Error('invalid byte sequence for encoding "UTF8": 0x00'),
      { code: "22021", severity: "ERROR" },
    );
    const error = Object.assign(
      new Error(
        'Failed query: update "smtp_configs" set "imap_last_sync_error" = $1\nparams: Dear customer\u0000, your secret order…',
      ),
      {
        query: 'update "smtp_configs" set "imap_last_sync_error" = $1 where "id" = $2',
        params: ["Dear customer\u0000, your secret order…", "id"],
        cause,
      },
    );

    const text = describeError(error);
    expect(text).toBe(
      'Database error 22021: invalid byte sequence for encoding "UTF8": 0x00 (update smtp_configs)',
    );
    expect(text).not.toContain("secret");
    expect(text).not.toContain("\u0000");
  });

  it("reports ImapFlow codes and the server's response text", () => {
    expect(
      describeError(Object.assign(new Error("Socket timeout"), { code: "ETIMEOUT" })),
    ).toBe("Socket timeout [ETIMEOUT]");
    expect(
      describeError(
        Object.assign(new Error("Command failed"), {
          responseText: "[THROTTLED] Account exceeded command or bandwidth limits",
        }),
      ),
    ).toBe(
      "Command failed - server: [THROTTLED] Account exceeded command or bandwidth limits",
    );
  });

  it("is a single bounded line without control characters", () => {
    const long = describeError(new Error(`a\r\nb\u0000c\t${"x".repeat(2000)}`), 100);
    expect(long.length).toBeLessThanOrEqual(100);
    expect(long.startsWith("a b c x")).toBe(true);
    expect(long.endsWith("…")).toBe(true);
    // eslint-disable-next-line no-control-regex
    expect(long).not.toMatch(/[\u0000-\u001f]/);
  });

  it("handles non-Error values", () => {
    expect(describeError("plain")).toBe("plain");
    expect(describeError(undefined)).toBe("undefined");
    expect(describeError({ message: "shaped like an error" })).toBe(
      "shaped like an error",
    );
  });
});

describe("stackFrames", () => {
  it("returns only the frames, never the message line", () => {
    const error = new Error("contains data: hello@example.test");
    const frames = stackFrames(error);
    expect(frames).toMatch(/^\s+at /);
    expect(frames).not.toContain("hello@example.test");
  });
});
