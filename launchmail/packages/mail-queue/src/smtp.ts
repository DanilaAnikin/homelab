import { createHash } from "node:crypto";
import nodemailer from "nodemailer";
import type { SmtpConfig } from "./smtp-configs.service";

const transportCache = new Map<string, nodemailer.Transporter>();

// Include a password fingerprint (never the plaintext) so a credential change
// yields a fresh transporter even when host/port/username are unchanged.
// Without this a rotated password would keep authenticating with the stale
// cached connection.
function getTransportKey(config: SmtpConfig): string {
  const fingerprint = createHash("sha256")
    .update(config.password)
    .digest("hex")
    .slice(0, 16);
  return `${config.id}:${config.host}:${config.port}:${config.username}:${fingerprint}`;
}

export function getTransport(config: SmtpConfig): nodemailer.Transporter {
  const key = getTransportKey(config);
  const cached = transportCache.get(key);
  if (cached) return cached;

  const transporter = nodemailer.createTransport({
    host: config.host,
    port: config.port,
    secure: config.port === 465,
    auth: {
      user: config.username,
      pass: config.password,
    },
  });

  transportCache.set(key, transporter);
  return transporter;
}

export function invalidateTransport(config: SmtpConfig): void {
  transportCache.delete(getTransportKey(config));
}

export interface SendMailInput {
  from: string;
  to: { email: string; name?: string }[];
  cc?: { email: string; name?: string }[];
  bcc?: { email: string; name?: string }[];
  replyTo?: string;
  subject: string;
  html?: string;
  text?: string;
  inReplyTo?: string;
  references?: string;
  attachments?: { filename: string; content: string; contentType?: string }[];
  smtpConfig: SmtpConfig;
  dkim?: { domainName: string; keySelector: string; privateKey: string };
}

function formatRecipients(
  recipients?: { email: string; name?: string }[],
): string[] | undefined {
  if (!recipients || recipients.length === 0) return undefined;
  return recipients.map((r) =>
    r.name ? `${r.name} <${r.email}>` : r.email,
  );
}

export async function sendMail(input: SendMailInput) {
  const transporter = getTransport(input.smtpConfig);

  // Use the authenticated account as the envelope sender (MAIL FROM /
  // Return-Path) when the SMTP username is an email address. Many providers
  // (Seznam, Gmail, …) reject if MAIL FROM isn't the login, even when the
  // visible From header is a different address.
  const login = input.smtpConfig.username;
  const useEnvelope = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(login);
  const envelopeTo = [
    ...(input.to ?? []),
    ...(input.cc ?? []),
    ...(input.bcc ?? []),
  ].map((r) => r.email);

  // Space-separated Message-Ids; drop empties so a blank/whitespace value never
  // sets an empty References header.
  const references = input.references?.split(/\s+/).filter(Boolean);

  const attachments = input.attachments?.length
    ? input.attachments.map((a) => ({
        filename: a.filename,
        content: Buffer.from(a.content, "base64"),
        contentType: a.contentType,
      }))
    : undefined;

  return transporter.sendMail({
    from: input.from,
    to: formatRecipients(input.to),
    cc: formatRecipients(input.cc),
    bcc: formatRecipients(input.bcc),
    replyTo: input.replyTo,
    subject: input.subject,
    html: input.html,
    text: input.text,
    // RFC 5322 threading headers (nodemailer maps these to In-Reply-To /
    // References). `references` is a space-separated list of Message-Ids.
    ...(input.inReplyTo ? { inReplyTo: input.inReplyTo } : {}),
    ...(references && references.length ? { references } : {}),
    ...(attachments ? { attachments } : {}),
    ...(useEnvelope
      ? { envelope: { from: login, to: envelopeTo } }
      : {}),
    ...(input.dkim ? { dkim: input.dkim } : {}),
  });
}

export async function testSmtpConnection(
  config: SmtpConfig,
): Promise<{ success: true } | { success: false; error: string }> {
  try {
    const transporter = nodemailer.createTransport({
      host: config.host,
      port: config.port,
      secure: config.port === 465,
      auth: {
        user: config.username,
        pass: config.password,
      },
      connectionTimeout: 10000,
    });
    await transporter.verify();
    transporter.close();
    return { success: true };
  } catch (err) {
    return {
      success: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}
