import { Queue } from "bullmq";
import { REDIS_URL } from "./redis";

export interface EmailJobData {
  smtpConfigId: string;
  organizationId?: string | null;
  userId?: string | null;
  from: string;
  to: { email: string; name?: string }[];
  cc?: { email: string; name?: string }[];
  bcc?: { email: string; name?: string }[];
  replyTo?: string;
  subject: string;
  html?: string;
  text?: string;
  // Threading: set when this send is a reply to a received message so the
  // recipient's client groups it into the original conversation.
  inReplyTo?: string;
  references?: string;
  // Outgoing attachments, base64-encoded (e.g. files added to a reply/forward).
  attachments?: { filename: string; content: string; contentType?: string }[];
}

// Pass URL string to avoid ioredis type version conflicts
export const mailQueue = new Queue<EmailJobData>("mail-queue", {
  connection: { url: REDIS_URL },
  defaultJobOptions: {
    attempts: 3,
    backoff: { type: "exponential", delay: 5000 },
    removeOnComplete: { age: 3600 * 24 },
    removeOnFail: { age: 3600 * 24 * 7 },
  },
});

export async function enqueueEmail(
  data: EmailJobData,
  opts?: { sendAt?: string | null },
) {
  let delay: number | undefined;
  if (opts?.sendAt) {
    const ts = new Date(opts.sendAt).getTime();
    if (!Number.isNaN(ts)) delay = Math.max(0, ts - Date.now());
  }
  return mailQueue.add("send-email", data, delay ? { delay } : undefined);
}
