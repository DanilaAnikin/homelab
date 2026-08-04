import { createHash, randomUUID } from "node:crypto";
import { Worker, DelayedError, type Job } from "bullmq";
import { REDIS_URL } from "./redis";
import { sendMail } from "./smtp";
import { getSmtpConfigById } from "./smtp-configs.service";
import { filterSuppressed, addSuppression } from "./suppressions.service";
import { injectTracking, trackingEnabled } from "./tracking.service";
import { findSigningDomain } from "./domains.service";
import {
  classifyDeliveryError,
  extractEmail,
  domainOf,
} from "./direct-transport";
import { mailBackoffDelay, MAIL_JOB_ATTEMPTS } from "./backoff";
import { consumeDomainToken } from "./rate-limiter";
import { consumeDailyQuota } from "./warmup";
import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, eq } from "drizzle-orm";
import type { EmailJobData } from "./queue";
import {
  finalizeEmailTerminal,
  type FinalizeEmailTerminalInput,
} from "./email-terminal";

// Derive a stable email-log id (UUIDv5-shaped) from the BullMQ job id so every
// retry of the same job reuses the same primary key. The 'sent' row is written
// with this id; a later attempt that finds a 'sent' row with this id skips the
// send entirely. This makes the send handler idempotent across retries.
export function dedupeLogId(jobId: string): string {
  const h = createHash("sha256").update(jobId).digest("hex");
  return [
    h.slice(0, 8),
    h.slice(8, 12),
    `5${h.slice(13, 16)}`,
    ((parseInt(h.slice(16, 17), 16) & 0x3) | 0x8).toString(16) +
      h.slice(17, 20),
    h.slice(20, 32),
  ].join("-");
}

// Only a genuine *recipient* failure (the mailbox doesn't exist) should suppress
// an address. Sender/policy/relay/auth/temporary rejections (e.g. 5.7.1
// "use your login email address as mail from", relaying denied, auth required,
// spam/greylist, rate limits, 4xx) must NEVER suppress the recipient.
export function isHardBounce(err: {
  responseCode?: number;
  message?: string;
}): boolean {
  const m = (err.message ?? "").toLowerCase();

  // Temporary failures (4xx) are never permanent bounces.
  if (err.responseCode && err.responseCode < 500) return false;

  // Sender/policy/relay/auth problems — not the recipient's fault.
  if (
    /\b5\.7\.|\b5\.4\.|\b4\.\d\.|relay|relaying|sender|mail from|not authoriz|authentication|auth required|spf|dkim|dmarc|policy|spam|blocked|blacklist|greylist|rate limit|too many|quota|try again|temporar|access denied/.test(
      m,
    )
  ) {
    return false;
  }

  // Unambiguous "recipient mailbox does not exist" signals.
  return /\b5\.1\.[0-3]\b|user unknown|unknown user|no such user|user not found|no such recipient|recipient not found|unknown recipient|invalid recipient|mailbox unavailable|mailbox not found|mailbox does not exist|no such mailbox|no mailbox|address (not found|does not exist|rejected.*user unknown)|account (does not exist|disabled|unavailable)/.test(
    m,
  );
}

export interface FailureHandlingDependencies {
  finalize: (input: FinalizeEmailTerminalInput) => Promise<unknown>;
  recordDeferred: (
    trackingId: string,
    job: EmailJobData,
    error: string,
  ) => Promise<void>;
  suppress: (organizationId: string, email: string) => Promise<void>;
}

const failureDependencies: FailureHandlingDependencies = {
  finalize: finalizeEmailTerminal,
  async recordDeferred(trackingId, job, error) {
    await db
      .insert(emailLogs)
      .values({
        id: trackingId,
        smtpConfigId: job.smtpConfigId,
        organizationId: job.organizationId ?? null,
        userId: job.userId ?? null,
        from: job.from,
        to: job.to,
        subject: job.subject,
        status: "deferred",
        html: job.html ?? null,
        text: job.text ?? null,
        inReplyTo: job.inReplyTo ?? null,
        clientReference: job.clientReference ?? null,
        clientType: job.clientType ?? null,
        error,
      })
      .onConflictDoUpdate({
        target: emailLogs.id,
        set: { status: "deferred", error },
      });
  },
  async suppress(organizationId, email) {
    await addSuppression(organizationId, email, "bounce");
  },
};

export interface EmailFailureHandlingResult {
  status: "deferred" | "failed" | "bounced";
  willRetry: boolean;
}

export async function finalizeSuppressedEmail(
  job: Job<EmailJobData>,
  trackingId: string,
  finalize: (
    input: FinalizeEmailTerminalInput,
  ) => Promise<unknown> = finalizeEmailTerminal,
): Promise<void> {
  await finalize({
    trackingId,
    jobId: String(job.id),
    job: job.data,
    status: "suppressed",
    event: "email.suppressed",
    error: "All recipients are suppressed",
    data: {
      jobId: String(job.id),
      logId: trackingId,
      smtpConfigId: job.data.smtpConfigId,
      to: job.data.to.map((recipient) => recipient.email),
      reason: "all_recipients_suppressed",
      clientReference: job.data.clientReference ?? null,
      clientType: job.data.clientType ?? null,
    },
  });
}

/**
 * One failure gate for SMTP and every pre-SMTP exception. Retryable attempts
 * only record `deferred`; exactly the final/permanent attempt emits a terminal
 * outbox event via finalizeEmailTerminal.
 */
export async function handleEmailProcessingFailure(
  job: Job<EmailJobData>,
  trackingId: string,
  recipients: EmailJobData["to"],
  error: unknown,
  dependencies: FailureHandlingDependencies = failureDependencies,
): Promise<EmailFailureHandlingResult> {
  if (error instanceof DelayedError) throw error;

  const deliveryError = error as { responseCode?: number; message?: string };
  const errorMessage = error instanceof Error ? error.message : String(error);
  const hardBounce = isHardBounce(deliveryError);
  const permanentPreSmtp = /^SMTP config .+ not found$/.test(errorMessage);
  const attemptsMade = job.attemptsStarted ?? 1;
  const maxAttempts = job.opts.attempts ?? MAIL_JOB_ATTEMPTS;
  const willRetry =
    !hardBounce &&
    !permanentPreSmtp &&
    attemptsMade < maxAttempts &&
    classifyDeliveryError(deliveryError).retryable;
  const status = hardBounce ? "bounced" : willRetry ? "deferred" : "failed";

  if (willRetry) {
    await dependencies.recordDeferred(trackingId, job.data, errorMessage);
  } else if (hardBounce) {
    await dependencies.finalize({
      trackingId,
      jobId: String(job.id),
      job: job.data,
      status: "bounced",
      event: "email.bounced",
      error: errorMessage,
      data: {
        jobId: String(job.id),
        logId: trackingId,
        smtpConfigId: job.data.smtpConfigId,
        to: recipients.map((recipient) => recipient.email),
        status: "synchronous_hard_bounce",
        diagnostic: errorMessage,
        messageId: null,
        clientReference: job.data.clientReference ?? null,
        clientType: job.data.clientType ?? null,
      },
    });
    if (job.data.organizationId) {
      for (const recipient of recipients) {
        await dependencies
          .suppress(job.data.organizationId, recipient.email)
          .catch(() => undefined);
      }
    }
  } else {
    await dependencies.finalize({
      trackingId,
      jobId: String(job.id),
      job: job.data,
      status: "failed",
      event: "email.failed",
      error: errorMessage,
      data: {
        jobId: String(job.id),
        logId: trackingId,
        smtpConfigId: job.data.smtpConfigId,
        to: recipients.map((recipient) => recipient.email),
        subject: job.data.subject,
        error: errorMessage,
        clientReference: job.data.clientReference ?? null,
        clientType: job.data.clientType ?? null,
      },
    });
  }

  console.error(
    `[worker] ${status}: ${job.id} (attempt ${attemptsMade}/${maxAttempts}): ${errorMessage}`,
  );
  return { status, willRetry };
}

export function startWorker() {
  const worker = new Worker<EmailJobData>(
    "mail-queue",
    async (job, token) => {
      const {
        smtpConfigId,
        organizationId,
        from,
        to,
        cc,
        bcc,
        replyTo,
        subject,
        html,
        text,
        inReplyTo,
        references,
        clientReference,
        clientType,
        headers,
        attachments,
      } = job.data;

      // Deterministic log id for this job: lets us detect a prior successful
      // send and skip a duplicate on retry.
      const trackingId = dedupeLogId(
        String(job.id ?? job.token ?? randomUUID()),
      );
      let recipients = to;

      try {
        // Idempotency guard: if this job already produced a 'sent' log, a retry
        // (e.g. triggered by a post-send logging failure) must NOT send again.
        const [already] = await db
          .select({
            id: emailLogs.id,
            providerMessageId: emailLogs.providerMessageId,
          })
          .from(emailLogs)
          .where(
            and(eq(emailLogs.id, trackingId), eq(emailLogs.status, "sent")),
          )
          .limit(1);
        if (already) {
          console.log(
            `[worker] Already sent: ${job.id} (log ${trackingId}) — skipping`,
          );
          return { messageId: already.providerMessageId, deduped: true };
        }

        const config = await getSmtpConfigById(smtpConfigId);
        if (!config) {
          throw new Error(`SMTP config ${smtpConfigId} not found`);
        }

        // Drop suppressed recipients (manual, bounced, complained).
        if (organizationId) {
          const { allowed, suppressed } = await filterSuppressed(
            organizationId,
            to.map((r) => r.email),
          );
          if (suppressed.length > 0) {
            recipients = to.filter((r) => allowed.includes(r.email));
            console.log(
              `[worker] Skipping suppressed: ${suppressed.join(", ")}`,
            );
          }
          if (recipients.length === 0) {
            await finalizeSuppressedEmail(job, trackingId);
            return { skipped: true };
          }
        }

        // Direct delivery throttling. Both checks re-delay the job (no retry
        // attempt consumed) rather than failing. Smarthost skips this — the relay
        // meters its own throughput and owns its reputation.
        if (config.type === "direct") {
          // 1) Warm-up: ramp a young sending domain's daily volume so it doesn't
          //    look like a spammer blasting from a cold IP.
          const sendDomain = domainOf(extractEmail(from));
          const warm = await consumeDailyQuota(sendDomain);
          if (!warm.allowed) {
            console.log(
              `[worker] Warm-up cap ${warm.cap}/day reached for ${sendDomain} → deferring to next day`,
            );
            await job.moveToDelayed(Date.now() + warm.resetInMs, token);
            throw new DelayedError();
          }

          // 2) Per-recipient-domain rate limit: keep bursts under the big
          //    providers' per-minute thresholds.
          const domains = new Set(
            [...recipients, ...(cc ?? []), ...(bcc ?? [])].map((r) =>
              r.email.slice(r.email.lastIndexOf("@") + 1).toLowerCase(),
            ),
          );
          let waitMs = 0;
          for (const d of domains) {
            if (!d) continue;
            const decision = await consumeDomainToken(d);
            if (!decision.allowed)
              waitMs = Math.max(waitMs, decision.retryAfterMs);
          }
          if (waitMs > 0) {
            console.log(
              `[worker] Rate-limited ${job.id} → re-delaying ${waitMs}ms`,
            );
            await job.moveToDelayed(Date.now() + waitMs, token);
            throw new DelayedError();
          }
        }

        const trackedHtml =
          html && trackingEnabled() ? injectTracking(html, trackingId) : html;

        // DKIM-sign with a verified sending domain when the From domain matches.
        const dkim = organizationId
          ? await findSigningDomain(organizationId, from).catch(() => null)
          : null;

        const result = await sendMail({
          from,
          to: recipients,
          cc,
          bcc,
          replyTo,
          subject,
          html: trackedHtml,
          text,
          inReplyTo,
          references,
          headers,
          attachments,
          smtpConfig: config,
          dkim: dkim ?? undefined,
          // VERP: encode the log id in the return-path so this message's async
          // bounces (Phase 3) map straight back to this row.
          returnPathToken: trackingId,
        });

        // The mail is already out the door. A terminal DB failure cannot safely
        // re-run SMTP, so log it loudly; a committed outbox row is independently
        // recovered by the relay scanner when Redis is unavailable.
        try {
          await finalizeEmailTerminal({
            trackingId,
            jobId: String(job.id),
            job: job.data,
            status: "sent",
            event: "email.sent",
            providerMessageId: result.messageId,
            dkimSigned: !!dkim,
            data: {
              jobId: String(job.id),
              logId: trackingId,
              smtpConfigId,
              to: recipients.map((recipient) => recipient.email),
              subject,
              messageId: result.messageId,
              clientReference: clientReference ?? null,
              clientType: clientType ?? null,
            },
          });
        } catch (logErr) {
          console.error(
            `[worker] Post-send terminal commit failed for ${job.id} (mail already sent, NOT retrying SMTP): ${
              logErr instanceof Error ? logErr.message : String(logErr)
            }`,
          );
        }

        console.log(
          `[worker] Sent: ${job.id} → ${recipients.map((r) => r.email).join(", ")} (${result.messageId})`,
        );
        return { messageId: result.messageId };
      } catch (error) {
        await handleEmailProcessingFailure(job, trackingId, recipients, error);
        throw error;
      }
    },
    {
      connection: { url: REDIS_URL },
      concurrency: 10,
      limiter: {
        max: 50,
        duration: 1000,
      },
      // Greylisting-friendly retry spacing (see backoff.ts). Job opts set
      // backoff.type = "custom" so this strategy is used.
      settings: {
        backoffStrategy: (attemptsMade: number) =>
          mailBackoffDelay(attemptsMade),
      },
    },
  );

  // Surface infrastructure-level errors (Redis drops, etc.) instead of letting
  // them crash the process as unhandled. BullMQ recovers on reconnect.
  worker.on("error", (err) => {
    console.error(`[worker] Worker error: ${err.message}`);
  });

  worker.on("completed", (job) => {
    console.log(`[worker] Completed: ${job.id}`);
  });

  worker.on("failed", (job, err) => {
    if (job) {
      console.error(`[worker] Permanently failed: ${job.id}: ${err.message}`);
    }
  });

  console.log("[worker] BullMQ worker started on queue 'mail-queue'");
  return worker;
}
