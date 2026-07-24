import { z } from "zod";

export const recipientSchema = z.object({
  email: z.string().email(),
  name: z.string().optional(),
});

const optionalRecipientArray = z
  .array(z.object({
    email: z.string(),
    name: z.string().optional(),
  }))
  .optional()
  .transform((arr) =>
    arr
      ?.filter((r) => r.email && z.string().email().safeParse(r.email).success)
      .map((r) => ({ email: r.email, name: r.name }))
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
});

export type SendEmailInput = z.infer<typeof sendEmailSchema>;
export type Recipient = z.infer<typeof recipientSchema>;
