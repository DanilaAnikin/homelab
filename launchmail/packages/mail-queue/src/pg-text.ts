// PostgreSQL-safe text for values that come from untrusted mail.
//
// `text`/`varchar` columns cannot hold U+0000: the server rejects the whole
// statement with SQLSTATE 22021 ("invalid byte sequence for encoding UTF8:
// 0x00"), and `jsonb` rejects the escaped form (22P05). A lone UTF-16
// surrogate is silently turned into U+FFFD for `text`, but inside `jsonb` the
// escaped surrogate is rejected too. Real mail carries both: a mis-declared
// UTF-16 HTML part decodes into thousands of NULs (20. 10. 2026: one such
// message wedged a whole mailbox for three weeks), and cutting a string in the
// middle of an emoji leaves a lone surrogate. Everything persisted from a
// parsed message therefore goes through toPgText / boundPgText.

// A high surrogate not followed by a low one, or a low one not preceded by a
// high one.
const LONE_SURROGATE =
  /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
const HAS_SURROGATE = /[\uD800-\uDFFF]/;

/** Remove NUL characters and replace lone surrogates with U+FFFD. */
export function toPgText(value: string): string {
  let out = value;
  if (out.includes("\u0000")) out = out.replaceAll("\u0000", "");
  if (HAS_SURROGATE.test(out)) out = out.replace(LONE_SURROGATE, "\uFFFD");
  return out;
}

export interface BoundedText {
  value: string;
  truncated: boolean;
}

/**
 * toPgText + cap at `max` UTF-16 code units without splitting a surrogate
 * pair (a split pair would itself become an invalid lone surrogate).
 * Removing NULs is a sanitisation, not a truncation, so it does not set
 * `truncated`.
 */
export function boundPgText(value: string, max: number): BoundedText {
  const clean = toPgText(value);
  if (clean.length <= max) return { value: clean, truncated: false };
  let cut = clean.slice(0, Math.max(0, max));
  const last = cut.charCodeAt(cut.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1);
  return { value: cut, truncated: true };
}

/**
 * SQLSTATE of a database error, looking through wrappers: drizzle-orm wraps
 * the driver error in DrizzleQueryError (`cause`). Only objects that look
 * like a node-postgres DatabaseError (5-character code + severity) count, so
 * Node system errors such as EPIPE are never mistaken for a SQLSTATE.
 */
export function pgErrorCode(error: unknown): string | undefined {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current != null; depth += 1) {
    if (typeof current === "object") {
      const candidate = current as {
        code?: unknown;
        severity?: unknown;
        cause?: unknown;
      };
      if (
        typeof candidate.code === "string" &&
        /^[0-9A-Z]{5}$/.test(candidate.code) &&
        typeof candidate.severity === "string"
      ) {
        return candidate.code;
      }
      current = candidate.cause;
    } else {
      return undefined;
    }
  }
  return undefined;
}

/**
 * True when the database rejected the VALUES of a statement (class 22 data
 * exception, class 23 integrity violation, class 54 program limit), i.e. the
 * same rows would fail again on retry. Connection loss, shutdown, timeouts,
 * serialization failures and anything without a SQLSTATE are NOT permanent:
 * those must abort the sync so the cursor stays put and the batch is retried.
 */
export function isPermanentRowError(error: unknown): boolean {
  const code = pgErrorCode(error);
  if (!code) return false;
  return code.startsWith("22") || code.startsWith("23") || code.startsWith("54");
}
