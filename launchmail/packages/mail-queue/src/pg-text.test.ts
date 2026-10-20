import { describe, expect, it } from "vitest";
import {
  boundPgText,
  isPermanentRowError,
  pgErrorCode,
  toPgText,
} from "./pg-text";

const pgError = (code: string) =>
  Object.assign(new Error("db said no"), { code, severity: "ERROR" });

describe("toPgText", () => {
  it("removes every NUL character", () => {
    expect(toPgText("<\u0000p\u0000>\u0000")).toBe("<p>");
    expect(toPgText("\u0000\u0000")).toBe("");
  });

  it("replaces lone surrogates (rejected inside jsonb) and keeps valid pairs", () => {
    expect(toPgText("a\uD83Db")).toBe("a\uFFFDb"); // lone high
    expect(toPgText("a\uDE00b")).toBe("a\uFFFDb"); // lone low
    expect(toPgText("end\uD83D")).toBe("end\uFFFD");
    expect(toPgText("smile \uD83D\uDE00")).toBe("smile \uD83D\uDE00");
  });

  it("returns ordinary text unchanged", () => {
    const text = "Příliš žluťoučký kůň — \t\r\n OK";
    expect(toPgText(text)).toBe(text);
  });
});

describe("boundPgText", () => {
  it("does not count NUL removal as truncation", () => {
    expect(boundPgText("a\u0000b", 10)).toEqual({ value: "ab", truncated: false });
  });

  it("bounds after cleaning and never splits a surrogate pair", () => {
    // "ab\uD83D\uDE00" is 4 code units; a cut at 3 would leave a lone high surrogate.
    expect(boundPgText("ab\uD83D\uDE00", 3)).toEqual({ value: "ab", truncated: true });
    expect(boundPgText("ab\uD83D\uDE00", 4)).toEqual({
      value: "ab\uD83D\uDE00",
      truncated: false,
    });
    expect(boundPgText("abcdef", 3)).toEqual({ value: "abc", truncated: true });
  });
});

describe("database error classification", () => {
  it("reads the SQLSTATE through drizzle's DrizzleQueryError wrapper", () => {
    const wrapped = Object.assign(new Error("Failed query: …\nparams: …"), {
      query: "insert …",
      params: [],
      cause: pgError("22021"),
    });
    expect(pgErrorCode(wrapped)).toBe("22021");
  });

  it("does not mistake a Node system error code for a SQLSTATE", () => {
    const epipe = Object.assign(new Error("write EPIPE"), { code: "EPIPE" });
    expect(pgErrorCode(epipe)).toBeUndefined();
    expect(isPermanentRowError(epipe)).toBe(false);
  });

  it("treats data, integrity and size errors as row-level (permanent)", () => {
    for (const code of ["22021", "22P05", "22001", "22008", "23502", "23514", "54000"]) {
      expect(isPermanentRowError(pgError(code))).toBe(true);
    }
  });

  it("treats connection, shutdown, concurrency and unknown errors as transient", () => {
    for (const code of ["08006", "57P01", "53300", "40001", "40P01", "42P01", "XX000"]) {
      expect(isPermanentRowError(pgError(code))).toBe(false);
    }
    expect(isPermanentRowError(new Error("Connection terminated"))).toBe(false);
    expect(isPermanentRowError(undefined)).toBe(false);
  });
});
