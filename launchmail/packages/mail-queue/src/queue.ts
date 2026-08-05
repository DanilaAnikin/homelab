import { Queue } from "bullmq";
import { REDIS_URL } from "./redis";
import { MAIL_JOB_ATTEMPTS } from "./backoff";
import type { LaunchMailClientType } from "./types";
import {
  buildEmailQueueJobOptions,
  type EmailQueueOptionsInput,
} from "./email-job-identity";

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
  /** Caller-owned durable correlation id, emitted unchanged in webhooks. */
  clientReference?: string;
  clientType?: LaunchMailClientType;
  /** Restricted RFC 8058 headers validated by sendEmailSchema. */
  headers?: {
    "List-Unsubscribe"?: string;
    "List-Unsubscribe-Post"?: "List-Unsubscribe=One-Click";
  };
  // Outgoing attachments, base64-encoded (e.g. files added to a reply/forward).
  attachments?: { filename: string; content: string; contentType?: string }[];
}

// Pass URL string to avoid ioredis type version conflicts
export const mailQueue = new Queue<EmailJobData>("mail-queue", {
  connection: { url: REDIS_URL },
  defaultJobOptions: {
    // Greylisting-friendly retries: the worker's custom backoffStrategy
    // (backoff.ts) spaces attempts 1m → 5m → 15m → 1h → 4h → 8h → 24h.
    attempts: MAIL_JOB_ATTEMPTS,
    backoff: { type: "custom" },
    removeOnComplete: { age: 3600 * 24 },
    removeOnFail: { age: 3600 * 24 * 7 },
  },
});

export async function enqueueEmail(
  data: EmailJobData,
  opts?: EmailQueueOptionsInput,
) {
  return mailQueue.add("send-email", data, buildEmailQueueJobOptions(opts));
}
