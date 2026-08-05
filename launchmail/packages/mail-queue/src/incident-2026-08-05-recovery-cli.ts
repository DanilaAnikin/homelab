#!/usr/bin/env node

import { db } from "@workspace/db"
import { emailLogs, webhooks } from "@workspace/db/schemas"
import { and, eq, inArray, or } from "drizzle-orm"
import { finalizeEmailTerminal } from "./email-terminal"
import {
  buildIncident20260805RecoveryPlan,
  executeIncident20260805Recovery,
  INCIDENT_2026_08_05_APPLY_ACK,
  INCIDENT_2026_08_05_JOB_IDS,
  Incident20260805RecoveryError,
  incident20260805Sha256,
  type Incident20260805JobSnapshot,
  validateIncident20260805Webhook,
} from "./incident-2026-08-05-recovery"
import { mailQueue } from "./queue"
import { closeRedis } from "./redis"
import { closeWebhookQueue } from "./webhook-queue"

type Mode = "dry-run" | "apply" | "help"

function usage(): string {
  return [
    "LaunchMail Freio incident recovery — 2026-08-05",
    "",
    "Dry-run (default; no writes):",
    "  pnpm exec tsx src/incident-2026-08-05-recovery-cli.ts",
    "  pnpm exec tsx src/incident-2026-08-05-recovery-cli.ts --dry-run",
    "",
    "Apply (terminal log + signed webhook outbox only; never SMTP):",
    "  pnpm exec tsx src/incident-2026-08-05-recovery-cli.ts --apply --ack " +
      INCIDENT_2026_08_05_APPLY_ACK,
  ].join("\n")
}

function parseMode(args: string[]): Mode {
  if (args.length === 0 || (args.length === 1 && args[0] === "--dry-run")) {
    return "dry-run"
  }
  if (args.length === 1 && (args[0] === "--help" || args[0] === "-h")) {
    return "help"
  }
  if (
    args.length === 3 &&
    args[0] === "--apply" &&
    args[1] === "--ack" &&
    args[2] === INCIDENT_2026_08_05_APPLY_ACK
  ) {
    return "apply"
  }
  throw new Incident20260805RecoveryError(
    "Invalid arguments. Apply requires the exact incident acknowledgement."
  )
}

async function loadIncidentJobs(): Promise<Incident20260805JobSnapshot[]> {
  const snapshots: Incident20260805JobSnapshot[] = []
  for (const id of INCIDENT_2026_08_05_JOB_IDS) {
    const job = await mailQueue.getJob(id)
    if (!job) {
      snapshots.push({
        id: null,
        state: "missing",
        data: null,
        returnvalue: null,
        finishedOn: null,
      })
      continue
    }
    snapshots.push({
      id: job.id,
      state: await job.getState(),
      data: job.data,
      returnvalue: job.returnvalue,
      finishedOn: job.finishedOn,
    })
  }
  return snapshots
}

async function closeConnections(): Promise<void> {
  await Promise.allSettled([mailQueue.close(), closeWebhookQueue()])
  closeRedis()
}

async function main(): Promise<number> {
  const mode = parseMode(process.argv.slice(2))
  if (mode === "help") {
    process.stdout.write(`${usage()}\n`)
    return 0
  }

  const snapshots = await loadIncidentJobs()

  // First pure pass validates the Redis evidence before any client reference
  // is used in a database predicate. The second pass validates durable state.
  const redisPlan = buildIncident20260805RecoveryPlan(snapshots, [])
  const enabledHooks = await db
    .select({
      id: webhooks.id,
      enabled: webhooks.enabled,
      events: webhooks.events,
    })
    .from(webhooks)
    .where(
      and(
        eq(webhooks.organizationId, redisPlan.organizationId),
        eq(webhooks.enabled, true)
      )
    )
  const webhookId = validateIncident20260805Webhook(enabledHooks)
  const clientReferences = redisPlan.items.map((item) => item.clientReference)
  const trackingIds = redisPlan.items.map((item) => item.trackingId)
  const existingLogs = await db
    .select({
      id: emailLogs.id,
      clientReference: emailLogs.clientReference,
      clientType: emailLogs.clientType,
      organizationId: emailLogs.organizationId,
      smtpConfigId: emailLogs.smtpConfigId,
      status: emailLogs.status,
      providerMessageId: emailLogs.providerMessageId,
    })
    .from(emailLogs)
    .where(
      or(
        inArray(emailLogs.clientReference, clientReferences),
        inArray(emailLogs.id, trackingIds)
      )
    )

  const plan = buildIncident20260805RecoveryPlan(snapshots, existingLogs)
  const sanitizedPlan = {
    mode,
    incident: "2026-08-05-freio-terminal-recovery-v1",
    smtpCalls: 0,
    organizationSha256: incident20260805Sha256(plan.organizationId),
    smtpConfigSha256: incident20260805Sha256(plan.smtpConfigId),
    webhookSha256: incident20260805Sha256(webhookId),
    pending: plan.items.filter((item) => !item.alreadyRecovered).length,
    alreadyRecovered: plan.items.filter((item) => item.alreadyRecovered).length,
    jobs: plan.items.map((item) => ({
      jobId: item.jobId,
      clientReferenceSha256: item.clientReferenceSha256,
      trackingId: item.trackingId,
      outcome: item.outcome,
      occurredAt: new Date(item.finishedOn).toISOString(),
      alreadyRecovered: item.alreadyRecovered,
    })),
  }

  if (mode === "dry-run") {
    process.stdout.write(`${JSON.stringify(sanitizedPlan, null, 2)}\n`)
    return 0
  }

  const results = await executeIncident20260805Recovery(
    plan,
    finalizeEmailTerminal
  )
  process.stdout.write(
    `${JSON.stringify({ ...sanitizedPlan, applied: true, results }, null, 2)}\n`
  )
  return 0
}

let exitCode = 1
try {
  exitCode = await main()
} catch (error) {
  if (error instanceof Incident20260805RecoveryError) {
    process.stderr.write(`Recovery refused: ${error.message}\n`)
  } else {
    // Unexpected provider/DB errors can contain sensitive connection or message
    // details. Keep CLI output non-sensitive and inspect protected service logs.
    process.stderr.write(
      "Recovery failed unexpectedly; inspect protected LaunchMail service logs.\n"
    )
  }
} finally {
  await closeConnections()
}

process.exit(exitCode)
