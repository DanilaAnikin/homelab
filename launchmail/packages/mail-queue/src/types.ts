import { z } from "zod";

export const recipientSchema = z.object({
  email: z.string().email(),
  name: z.string().optional(),
});

export const LAUNCHMAIL_CLIENT_TYPES = [
  "freio_b2b_outreach",
  "freio_partner_outreach",
  "freio_transactional_outbox",
  "freio_lifecycle",
  "freio_inbox_reply",
] as const;
export const clientTypeSchema = z.enum(LAUNCHMAIL_CLIENT_TYPES);
export type LaunchMailClientType = (typeof LAUNCHMAIL_CLIENT_TYPES)[number];

const optionalRecipientArray = z
  .array(
    z.object({
      email: z.string(),
      name: z.string().optional(),
    }),
  )
  .optional()
  .transform((arr) =>
    arr
      ?.filter((r) => r.email && z.string().email().safeParse(r.email).success)
      .map((r) => ({ email: r.email, name: r.name })),
  );

export const sendEmailSchema = z.object({
  from: z.string().optional(),
  to: z.array(recipientSchema).min(1),
  cc: optionalRecipientArray,
  bcc: optionalRecipientArray,
  subject: z.string().min(1),
  html: z.string().optional(),
  text: z.string().optional(),
  // File attachments. `content` is the file bytes base64-encoded; the SMTP
  // transport decodes it to a Buffer for nodemailer. The queue/worker/transport
  // already carry attachments through — this just exposes them on the public
  // send API (e.g. LaunchMail sending an audit-report PDF for Lokwave).
  attachments: z
    .array(
      z.object({
        filename: z.string().min(1),
        content: z.string(),
        contentType: z.string().optional(),
      }),
    )
    .max(20)
    .optional(),
  sendAt: z.string().datetime().optional(),
  // Threading headers (RFC 5322) so autonomous replies land in the original
  // conversation. `references` is a space-separated list of Message-Ids. The
  // queue/worker/transports already honour these — this exposes them on the
  // public send API (Lokwave email bot replying in-thread).
  inReplyTo: z.string().optional(),
  references: z.string().optional(),
  // Opaque caller correlation carried into terminal webhooks. A UUID keeps
  // the value bounded and lets durable caller ledgers correlate an event even
  // when the worker outruns the HTTP response that returned the queue job id.
  clientReference: z.string().uuid().optional(),
  /** Non-PII caller namespace used to route shared-mailbox webhooks. */
  clientType: clientTypeSchema.optional(),
  // Deliberately allow only the two RFC 8058 marketing headers needed by
  // trusted callers. A generic arbitrary-header bag would let API clients
  // override routing/identity headers such as From, To or Subject.
  headers: z
    .object({
      "List-Unsubscribe": z.string().min(1).max(2048).optional(),
      "List-Unsubscribe-Post": z
        .literal("List-Unsubscribe=One-Click")
        .optional(),
    })
    .strict()
    .optional(),
});

export type SendEmailInput = z.infer<typeof sendEmailSchema>;
export type Recipient = z.infer<typeof recipientSchema>;
