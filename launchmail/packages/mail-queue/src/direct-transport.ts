// ============================================================================
// Direct MX delivery — launchmail as its own MTA (no upstream relay).
//
// For an smtp_config of type "direct" we resolve each recipient domain's MX
// records ourselves and speak SMTP to them on port 25, opportunistic STARTTLS,
// DKIM-signed. This is what makes launchmail a real ESP instead of a wrapper
// around Resend/SES.
//
// Hard requirements the code CANNOT paper over (enforced or documented):
//   • The egress IP must have a PTR (reverse DNS) matching `heloHostname`, and
//     SPF/DKIM/DMARC must be published for the sending domain. Without these,
//     Gmail/Outlook/Seznam reject regardless of this code. Verification of the
//     domain (which mints DKIM) is required before a direct send is attempted.
//   • Outbound port 25 must be open from the host running this (the delivery
//     node). Residential lines usually lack a controllable PTR — hence the
//     dedicated egress node in DIRECT_DELIVERY_PLAN.md, Phase 5.
//
// This module is pure of app state: it takes a SendMailInput and returns a
// message id or throws a classified error. The worker owns retries/suppression.
// ============================================================================
import { resolveMx } from "node:dns/promises";
import nodemailer from "nodemailer";
import type { SendMailInput } from "./smtp";
import { verpReturnPath } from "./bounce";

// Receiving MTAs are patient with greetings but we don't want a dead MX to pin
// a worker slot. 20 s each is generous for a real server, fatal for a black hole.
const PORT_25 = 25;
const CONNECTION_TIMEOUT_MS = 20_000;
const GREETING_TIMEOUT_MS = 20_000;
const SOCKET_TIMEOUT_MS = 60_000;

// ── Pure helpers (unit-tested) ──────────────────────────────────────────────

/** Extract the bare address from a "Name <addr@x>" header value, lowercased. */
export function extractEmail(address: string): string {
  const angled = address.match(/<([^>]+)>/);
  return (angled ? angled[1]! : address).trim().toLowerCase();
}

/** Domain part of an email address (lowercased), or "" if malformed. */
export function domainOf(email: string): string {
  const at = email.lastIndexOf("@");
  return at === -1 ? "" : email.slice(at + 1).trim().toLowerCase();
}

/**
 * Group recipient addresses by their domain so we open one SMTP transaction
 * per receiving system. Malformed addresses (no domain) are dropped.
 */
export function groupRecipientsByDomain(
  emails: string[],
): Map<string, string[]> {
  const byDomain = new Map<string, string[]>();
  for (const raw of emails) {
    const email = extractEmail(raw);
    const domain = domainOf(email);
    if (!domain) continue;
    const list = byDomain.get(domain);
    if (list) {
      if (!list.includes(email)) list.push(email);
    } else {
      byDomain.set(domain, [email]);
    }
  }
  return byDomain;
}

export interface DeliveryErrorInfo {
  /** true → transient (4xx / network); worker should retry the whole job. */
  retryable: boolean;
  /** SMTP response code when the failure came from the far end. */
  code?: number;
}

/**
 * Map a delivery failure to retryable vs permanent.
 *   • network errors (timeout, refused, reset, DNS) → retryable
 *   • SMTP 4xx (greylist, rate limit, mailbox busy)  → retryable
 *   • SMTP 5xx (user unknown, policy reject)         → permanent
 *   • unknown shape                                  → retryable (assume transient)
 */
export function classifyDeliveryError(err: unknown): DeliveryErrorInfo {
  const e = err as { responseCode?: number; code?: string };
  const NET = new Set([
    "ETIMEDOUT",
    "ECONNREFUSED",
    "ECONNRESET",
    "ESOCKET",
    "ECONNECTION",
    "EDNS",
    "EAI_AGAIN",
    "ENOTFOUND",
    "EENVELOPE",
    "ETLS",
  ]);
  if (e.code && NET.has(e.code)) return { retryable: true };
  if (typeof e.responseCode === "number") {
    return { retryable: e.responseCode < 500, code: e.responseCode };
  }
  return { retryable: true };
}

export interface MxResolver {
  resolveMx: (
    hostname: string,
  ) => Promise<{ exchange: string; priority: number }[]>;
}

/**
 * Ordered list of MX hosts to try for a domain, most-preferred first.
 * Falls back to the domain itself (implicit MX, RFC 5321 §5.1) when no MX
 * records exist — many small domains accept mail directly on their A record.
 */
export async function resolveMxHosts(
  domain: string,
  resolver: MxResolver = { resolveMx },
): Promise<string[]> {
  try {
    const records = await resolver.resolveMx(domain);
    const hosts = [...records]
      .sort((a, b) => a.priority - b.priority)
      .map((r) => r.exchange.trim())
      .filter(Boolean);
    if (hosts.length > 0) return hosts;
  } catch {
    // ENOTFOUND / ENODATA → fall through to implicit MX below.
  }
  return [domain];
}

// ── Message assembly (mirrors smtp.ts formatting, kept local to avoid an
//    import cycle with smtp.ts) ──────────────────────────────────────────────

function formatRecipients(
  recipients?: { email: string; name?: string }[],
): string[] | undefined {
  if (!recipients || recipients.length === 0) return undefined;
  return recipients.map((r) => (r.name ? `${r.name} <${r.email}>` : r.email));
}

function buildMessage(
  input: SendMailInput,
  dkim: NonNullable<SendMailInput["dkim"]>,
) {
  const references = input.references?.split(/\s+/).filter(Boolean);
  const attachments = input.attachments?.length
    ? input.attachments.map((a) => ({
        filename: a.filename,
        content: Buffer.from(a.content, "base64"),
        contentType: a.contentType,
      }))
    : undefined;
  return {
    from: input.from,
    to: formatRecipients(input.to),
    cc: formatRecipients(input.cc),
    bcc: formatRecipients(input.bcc),
    replyTo: input.replyTo,
    subject: input.subject,
    html: input.html,
    text: input.text,
    ...(input.inReplyTo ? { inReplyTo: input.inReplyTo } : {}),
    ...(references && references.length ? { references } : {}),
    ...(attachments ? { attachments } : {}),
    dkim,
  };
}

function permanent(message: string, code = 550): Error {
  return Object.assign(new Error(message), { responseCode: code });
}

// ── Main entry ──────────────────────────────────────────────────────────────

/**
 * Deliver one message directly to recipients' MX servers.
 * Returns the last accepted message id, or throws a classified error:
 *   • responseCode < 500  → worker retries the whole job
 *   • responseCode >= 500 → permanent; worker's hard-bounce logic may suppress
 *
 * Phase-1 scope: recipients are grouped by domain and each domain gets one
 * transaction. For a job spanning multiple domains a partial failure is thrown
 * as retryable, which can re-deliver to already-accepted domains on retry — a
 * documented limitation (single-recipient transactional sends, the common case,
 * are exact). Per-recipient fan-out is a later refinement (DIRECT_DELIVERY_PLAN).
 */
export async function sendDirect(
  input: SendMailInput,
): Promise<{ messageId: string }> {
  const { smtpConfig, dkim } = input;

  // Unsigned direct mail is dead on arrival at every major provider. Refuse to
  // attempt it — the operator must verify the sending domain (which creates the
  // DKIM key) first. This surfaces as a clear permanent failure in email logs.
  if (!dkim) {
    throw permanent(
      "direct delivery requires a verified sending domain (DKIM). " +
        `Verify the From domain (${domainOf(extractEmail(input.from))}) before sending.`,
    );
  }

  const helo = smtpConfig.heloHostname?.trim();
  if (!helo) {
    throw permanent(
      "direct SMTP config is missing heloHostname — set it to the delivery " +
        "node's hostname whose PTR matches the egress IP.",
    );
  }

  // Envelope return-path aligned to the From domain so SPF authenticates and
  // DMARC aligns. With a VERP token (the email-log id) the return-path becomes
  // bounces+<id>@domain, so an async DSN maps back to the exact message.
  const fromDomain = domainOf(extractEmail(input.from));
  const returnPath = input.returnPathToken
    ? verpReturnPath(fromDomain, input.returnPathToken)
    : extractEmail(input.from);
  const envelopeRecipients = [
    ...(input.to ?? []),
    ...(input.cc ?? []),
    ...(input.bcc ?? []),
  ].map((r) => r.email);

  const byDomain = groupRecipientsByDomain(envelopeRecipients);
  if (byDomain.size === 0) throw permanent("no valid recipients");

  const message = buildMessage(input, dkim);

  let lastMessageId = "";
  const failures: { domain: string; info: DeliveryErrorInfo; message: string }[] =
    [];

  for (const [domain, recipients] of byDomain) {
    const hosts = await resolveMxHosts(domain);
    let delivered = false;
    let lastErr: unknown;

    for (const host of hosts) {
      const transporter = nodemailer.createTransport({
        host,
        port: PORT_25,
        secure: false, // upgrade to TLS opportunistically via STARTTLS
        name: helo, // EHLO identity — must match egress PTR
        opportunisticTLS: true,
        tls: { rejectUnauthorized: false, minVersion: "TLSv1.2" },
        connectionTimeout: CONNECTION_TIMEOUT_MS,
        greetingTimeout: GREETING_TIMEOUT_MS,
        socketTimeout: SOCKET_TIMEOUT_MS,
      });
      try {
        const result = await transporter.sendMail({
          ...message,
          envelope: { from: returnPath, to: recipients },
        });
        lastMessageId = result.messageId;
        delivered = true;
        break;
      } catch (err) {
        lastErr = err;
        // A permanent 5xx from this MX will be repeated by the backup MX (same
        // mailbox) — stop and record it. A transient/network error is worth
        // trying the next MX host for.
        if (!classifyDeliveryError(err).retryable) break;
      } finally {
        transporter.close();
      }
    }

    if (!delivered) {
      const info = classifyDeliveryError(lastErr);
      failures.push({
        domain,
        info,
        message: lastErr instanceof Error ? lastErr.message : String(lastErr),
      });
    }
  }

  if (failures.length === 0) return { messageId: lastMessageId };

  // One error back to the worker. Any retryable failure → throw retryable so
  // BullMQ retries. Only when every failure is permanent do we throw 5xx (which
  // lets the worker auto-suppress genuinely dead recipients).
  const anyRetryable = failures.some((f) => f.info.retryable);
  const summary = failures.map((f) => `${f.domain}: ${f.message}`).join("; ");
  throw Object.assign(new Error(`direct delivery failed → ${summary}`), {
    responseCode: anyRetryable ? 421 : 550,
  });
}
