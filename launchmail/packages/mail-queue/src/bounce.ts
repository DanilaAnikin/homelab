// ============================================================================
// Bounce (DSN) parsing — turns "mail delivery failed" reports arriving in an
// IMAP mailbox into structured bounces so we can auto-suppress dead addresses
// and mark the original message bounced.
//
// Two things are extracted:
//   • VERP token — we send with Return-Path bounces+<logId>@domain, so a DSN's
//     recipient (To) carries the exact email_logs id it belongs to.
//   • RFC 3464 delivery-status fields — Final-Recipient, Action, Status,
//     Diagnostic-Code — parsed from the message/delivery-status part (surfaced
//     by mailparser as an attachment) with a fallback to the plaintext body.
// Pure module (no DB/network) → unit-tested.
// ============================================================================
import type { ParsedMail } from "mailparser";

// ── VERP (Variable Envelope Return Path) ────────────────────────────────────

/** Return-Path for a message so its bounces map back to the exact log row. */
export function verpReturnPath(domain: string, logId: string): string {
  return `bounces+${logId}@${domain.toLowerCase()}`;
}

/** Extract the <logId> from a bounces+<logId>@domain address, or null. */
export function parseVerpToken(address: string | undefined | null): string | null {
  if (!address) return null;
  const m = address.toLowerCase().match(/bounces\+([^@>\s]+)@/);
  return m ? m[1]! : null;
}

// ── DSN detection + field extraction ────────────────────────────────────────

export interface BounceResult {
  isBounce: boolean;
  /** true = permanent (5.x.x / failed) → suppress; false = transient/unknown. */
  permanent: boolean;
  recipient?: string;
  status?: string; // e.g. "5.1.1"
  diagnostic?: string;
  verpToken?: string;
}

const DAEMON_RE =
  /(mailer-daemon|postmaster|mail delivery (subsystem|system)|delivery.subsystem)/i;
const SUBJECT_RE =
  /(delivery status notification|undelivered mail|mail delivery (failed|failure)|returned mail|delivery (has )?failed|failure notice|undeliverable|returned to sender)/i;

function firstAddress(
  a: ParsedMail["from"] | ParsedMail["to"],
): string | undefined {
  if (!a) return undefined;
  const obj = Array.isArray(a) ? a[0] : a;
  return obj?.value?.[0]?.address ?? undefined;
}

/** Concatenate the machine-readable delivery-status part(s) + plaintext body. */
function bounceHaystack(parsed: ParsedMail): string {
  const parts: string[] = [];
  for (const att of parsed.attachments ?? []) {
    const ct = (att.contentType ?? "").toLowerCase();
    if (
      ct.includes("delivery-status") ||
      ct.includes("rfc822-headers") ||
      ct.includes("message/rfc822")
    ) {
      try {
        parts.push(att.content.toString("utf8"));
      } catch {
        /* ignore undecodable part */
      }
    }
  }
  if (parsed.text) parts.push(parsed.text);
  return parts.join("\n");
}

function match(haystack: string, re: RegExp): string | undefined {
  const m = haystack.match(re);
  return m ? m[1] : undefined;
}

/** Parse a message into a bounce verdict. Non-bounces → { isBounce:false }. */
export function parseBounce(parsed: ParsedMail): BounceResult {
  const fromAddr = (firstAddress(parsed.from) ?? "").toLowerCase();
  const subject = parsed.subject ?? "";
  const contentType = String(
    parsed.headers?.get?.("content-type") ?? "",
  ).toLowerCase();

  const haystack = bounceHaystack(parsed);

  const looksLikeBounce =
    /multipart\/report/.test(contentType) ||
    DAEMON_RE.test(fromAddr) ||
    fromAddr === "" || // null sender <> is the classic bounce envelope
    SUBJECT_RE.test(subject);

  const status = match(haystack, /Status:\s*([245]\.\d+\.\d+)/i);
  const action = match(haystack, /Action:\s*(\w+)/i)?.toLowerCase();
  const recipient =
    match(
      haystack,
      /(?:Final|Original)-Recipient:\s*[^;\n]*;\s*<?([^\s<>]+@[^\s<>]+?)>?\s*$/im,
    )?.toLowerCase();
  const diagnostic = match(haystack, /Diagnostic-Code:\s*([^\n]+)/i)?.trim();
  const verpToken = parseVerpToken(firstAddress(parsed.to)) ?? undefined;

  const isBounce = looksLikeBounce || !!status || !!verpToken;
  if (!isBounce) return { isBounce: false, permanent: false };

  // Permanent iff an explicit 5.x.x status, or Action: failed without a
  // transient 4.x.x status. A 4.x.x status is always treated as transient.
  const transient = status?.startsWith("4") ?? false;
  const permanent =
    !transient && ((status?.startsWith("5") ?? false) || action === "failed");

  return {
    isBounce: true,
    permanent,
    recipient,
    status,
    diagnostic,
    verpToken,
  };
}
