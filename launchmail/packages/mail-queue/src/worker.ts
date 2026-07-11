import { createHash, randomUUID } from "node:crypto";
import { Worker, DelayedError } from "bullmq";
import { REDIS_URL } from "./redis";
import { sendMail } from "./smtp";
import { getSmtpConfigById } from "./smtp-configs.service";
import { filterSuppressed, addSuppression } from "./suppressions.service";
import { dispatchEvent } from "./webhooks.service";
import { injectTracking, trackingEnabled } from "./tracking.service";
import { findSigningDomain } from "./domains.service";
import { classifyDeliveryError, extractEmail, domainOf } from "./direct-transport";
import { mailBackoffDelay, MAIL_JOB_ATTEMPTS } from "./backoff";
import { consumeDomainToken } from "./rate-limiter";
import { consumeDailyQuota } from "./warmup";
import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, eq } from "drizzle-orm";
import type { EmailJobData } from "./queue";

// Derive a stable email-log id (UUIDv5-shaped) from the BullMQ job id so every
// retry of the same job reuses the same primary key. The 'sent' row is written
// with this id; a later attempt that finds a 'sent' row with this id skips the
// send entirely. This makes the send handler idempotent across retries.
function dedupeLogId(jobId: string): string {
  const h = createHash("sha256").update(jobId).digest("hex");
  return [
    h.slice(0, 8),
    h.slice(8, 12),
    `5${h.slice(13, 16)}`,
    ((parseInt(h.slice(16, 17), 16) & 0x3) | 0x8).toString(16) + h.slice(17, 20),
    h.slice(20, 32),
  ].join("-");
}

// Only a genuine *recipient* failure (the mailbox doesn't exist) should suppress
// an address. Sender/policy/relay/auth/temporary rejections (e.g. 5.7.1
// "use your login email address as mail from", relaying denied, auth required,
// spam/greylist, rate limits, 4xx) must NEVER suppress the recipient.
function isHardBounce(err: { responseCode?: number; message?: string }): boolean {
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

export function startWorker() {
  const worker = new Worker<EmailJobData>(
    "mail-queue",
    async (job, token) => {
      const {
        smtpConfigId,
        organizationId,
        userId,
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
        attachments,
      } = job.data;

      // Deterministic log id for this job: lets us detect a prior successful
      // send and skip a duplicate on retry.
      const trackingId = dedupeLogId(String(job.id ?? job.token ?? randomUUID()));

      // Idempotency guard: if this job already produced a 'sent' log, a retry
      // (e.g. triggered by a post-send logging failure) must NOT send again.
      const [already] = await db
        .select({ id: emailLogs.id, providerMessageId: emailLogs.providerMessageId })
        .from(emailLogs)
        .where(and(eq(emailLogs.id, trackingId), eq(emailLogs.status, "sent")))
        .limit(1);
      if (already) {
        console.log(`[worker] Already sent: ${job.id} (log ${trackingId}) — skipping`);
        return { messageId: already.providerMessageId, deduped: true };
      }

      const config = await getSmtpConfigById(smtpConfigId);
      if (!config) {
        throw new Error(`SMTP config ${smtpConfigId} not found`);
      }

      // Drop suppressed recipients (manual, bounced, complained).
      let recipients = to;
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
          await db.insert(emailLogs).values({
            smtpConfigId,
            organizationId: organizationId ?? null,
            userId: userId ?? null,
            from,
            to,
            subject,
            status: "suppressed",
            html: html ?? null,
            text: text ?? null,
            inReplyTo: inReplyTo ?? null,
            error: "All recipients are suppressed",
          });
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
          if (!decision.allowed) waitMs = Math.max(waitMs, decision.retryAfterMs);
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

      try {
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
          attachments,
          smtpConfig: config,
          dkim: dkim ?? undefined,
          // VERP: encode the log id in the return-path so this message's async
          // bounces (Phase 3) map straight back to this row.
          returnPathToken: trackingId,
        });

        // The mail is already out the door. Any failure past this point must
        // NOT re-throw: throwing would re-run the handler and double-send. Log
        // best-effort and swallow errors so the job still completes.
        try {
          await db.insert(emailLogs).values({
            id: trackingId,
            smtpConfigId,
            organizationId: organizationId ?? null,
            userId: userId ?? null,
            from,
            to,
            subject,
            status: "sent",
            html: html ?? null,
            text: text ?? null,
            inReplyTo: inReplyTo ?? null,
            providerMessageId: result.messageId,
            dkimSigned: !!dkim,
          });

          if (organizationId) {
            void dispatchEvent(organizationId, "email.sent", {
              to: recipients.map((r) => r.email),
              subject,
              messageId: result.messageId,
            }).catch(() => undefined);
          }
        } catch (logErr) {
          console.error(
            `[worker] Post-send logging failed for ${job.id} (mail already sent, NOT retrying): ${
              logErr instanceof Error ? logErr.message : String(logErr)
            }`,
          );
        }

        console.log(
          `[worker] Sent: ${job.id} → ${recipients.map((r) => r.email).join(", ")} (${result.messageId})`,
        );
        return { messageId: result.messageId };
      } catch (err) {
        const smtpErr = err as { responseCode?: number; message?: string };
        const errMessage =
          err instanceof Error ? err.message : String(err);

        const hardBounce = isHardBounce(smtpErr);
        const attemptsMade = job.attemptsStarted ?? 1;
        const maxAttempts = job.opts.attempts ?? MAIL_JOB_ATTEMPTS;
        // Will BullMQ retry this job? Only if attempts remain AND the error is
        // transient (network / 4xx). A hard bounce or an out-of-attempts send
        // is terminal.
        const willRetry =
          !hardBounce &&
          attemptsMade < maxAttempts &&
          classifyDeliveryError(smtpErr).retryable;
        const status = hardBounce
          ? "bounced"
          : willRetry
            ? "deferred"
            : "failed";

        // One log row per message (keyed by trackingId), updated each attempt:
        // deferred → deferred → … → failed/bounced. Avoids a row per retry.
        await db
          .insert(emailLogs)
          .values({
            id: trackingId,
            smtpConfigId,
            organizationId: organizationId ?? null,
            userId: userId ?? null,
            from,
            to,
            subject,
            status,
            html: html ?? null,
            text: text ?? null,
            inReplyTo: inReplyTo ?? null,
            error: errMessage,
          })
          .onConflictDoUpdate({
            target: emailLogs.id,
            set: { status, error: errMessage },
          });

        if (organizationId && !willRetry) {
          void dispatchEvent(organizationId, "email.failed", {
            to: recipients.map((r) => r.email),
            subject,
            error: errMessage,
          }).catch(() => undefined);
        }

        // Auto-suppress recipients on a hard bounce to protect reputation.
        if (organizationId && hardBounce) {
          for (const r of recipients) {
            await addSuppression(organizationId, r.email, "bounce").catch(
              () => undefined,
            );
          }
        }

        console.error(
          `[worker] ${status}: ${job.id} (attempt ${attemptsMade}/${maxAttempts}): ${errMessage}`,
        );
        throw err;
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
        backoffStrategy: (attemptsMade: number) => mailBackoffDelay(attemptsMade),
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
