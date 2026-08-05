import { createHash } from "node:crypto"
import type { EmailJobData } from "./queue"
import {
  type FinalizeEmailTerminalInput,
  type FinalizeEmailTerminalResult,
} from "./email-terminal"
import { emailLogIdForJob } from "./email-job-identity"

export const INCIDENT_2026_08_05_APPLY_ACK =
  "APPLY_FREIO_2026_08_05_TERMINAL_RECOVERY_NO_SMTP"

export const INCIDENT_2026_08_05_JOB_IDS = ["1", "2", "3", "4", "5"] as const

export type Incident20260805JobId = (typeof INCIDENT_2026_08_05_JOB_IDS)[number]

const EXPECTED_CLIENT_REFERENCE_SHA256: Record<Incident20260805JobId, string> =
  {
    "1": "7b82845723f90524fadad53901fe4542e9e44cc1a79cd8072c04211f59880db9",
    "2": "f532c2a6e854d148d466123bcce4fcf4f4a6da48884f0b3fbe8990e628cb10a2",
    "3": "fad9d1d296069006aaf7687e652c401fd990de86802edc92346bb8702a33796f",
    "4": "1a6396e3c67a1c7ec300c66cc2578f04b52946f20ac42b62df54dc5c78ccca17",
    "5": "eab7a492d81a66b19e16f0c4b5270217f3efdeb327ecafe9c441e9f3b66f4391",
  }

const INCIDENT_TRACKING_NAMESPACE = "incident-recovery:v1:freio:2026-08-05:"
const INCIDENT_CLIENT_TYPE = "freio_b2b_outreach"
const INTERNAL_FALSE_DEDUPE_ERROR =
  "LaunchMail internal false deduplication after Redis sequence reset; SMTP was not attempted"
const INCIDENT_FINISHED_ON_MIN = Date.parse("2026-08-05T08:19:00.000Z")
const INCIDENT_FINISHED_ON_MAX = Date.parse("2026-08-05T08:22:00.000Z")
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

type JsonRecord = Record<string, unknown>

export interface Incident20260805JobSnapshot {
  id: string | null | undefined
  state: string
  data: unknown
  returnvalue: unknown
  finishedOn: number | null | undefined
}

export interface Incident20260805ExistingLog {
  id: string
  clientReference: string | null
  clientType: string | null
  organizationId: string | null
  smtpConfigId: string | null
  status: string
  providerMessageId: string | null
}

export interface Incident20260805RecoveryPlanItem {
  jobId: Incident20260805JobId
  clientReference: string
  clientReferenceSha256: string
  trackingId: string
  job: EmailJobData
  finishedOn: number
  outcome: "sent" | "failed"
  providerMessageId: string | null
  alreadyRecovered: boolean
}

export interface Incident20260805RecoveryPlan {
  organizationId: string
  smtpConfigId: string
  items: Incident20260805RecoveryPlanItem[]
}

export interface Incident20260805RecoveryPlanOptions {
  /** Test seam; production callers omit this and use the pinned incident hashes. */
  expectedClientReferenceSha256?: Readonly<
    Record<Incident20260805JobId, string>
  >
}

export interface Incident20260805WebhookSnapshot {
  id: string
  enabled: boolean
  events: unknown
}

export interface Incident20260805RecoveryResult {
  jobId: Incident20260805JobId
  clientReferenceSha256: string
  trackingId: string
  outcome: "sent" | "failed"
  deduplicated: boolean
  outboxRows: number
  skipped: boolean
}

export class Incident20260805RecoveryError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "Incident20260805RecoveryError"
  }
}

function fail(message: string): never {
  throw new Incident20260805RecoveryError(message)
}

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0
}

export function incident20260805Sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex")
}

export function incident20260805TrackingId(clientReference: string): string {
  return emailLogIdForJob(`${INCIDENT_TRACKING_NAMESPACE}${clientReference}`)
}

function parseReturnValue(value: unknown, jobId: string): JsonRecord {
  if (isRecord(value)) return value
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown
      if (isRecord(parsed)) return parsed
    } catch {
      // The fail-closed error below deliberately avoids echoing the payload.
    }
  }
  return fail(`Incident job ${jobId} has an invalid return value`)
}

function validateRecipient(value: unknown, jobId: string): void {
  if (!isRecord(value) || !nonEmptyString(value.email)) {
    fail(`Incident job ${jobId} has an invalid recipient`)
  }
}

function validateJobData(value: unknown, jobId: string): EmailJobData {
  if (!isRecord(value)) fail(`Incident job ${jobId} has invalid data`)
  if (!nonEmptyString(value.organizationId)) {
    fail(`Incident job ${jobId} is missing organizationId`)
  }
  if (
    !nonEmptyString(value.smtpConfigId) ||
    !UUID_PATTERN.test(value.smtpConfigId)
  ) {
    fail(`Incident job ${jobId} has an invalid smtpConfigId`)
  }
  if (
    !nonEmptyString(value.clientReference) ||
    !UUID_PATTERN.test(value.clientReference)
  ) {
    fail(`Incident job ${jobId} has an invalid clientReference`)
  }
  if (value.clientType !== INCIDENT_CLIENT_TYPE) {
    fail(`Incident job ${jobId} has an unexpected clientType`)
  }
  if (!nonEmptyString(value.from) || !nonEmptyString(value.subject)) {
    fail(`Incident job ${jobId} is missing immutable message data`)
  }
  if (!Array.isArray(value.to) || value.to.length !== 1) {
    fail(`Incident job ${jobId} must have exactly one recipient`)
  }
  validateRecipient(value.to[0], jobId)
  return value as unknown as EmailJobData
}

function expectedOutcome(jobId: Incident20260805JobId): "sent" | "failed" {
  return jobId === "1" ? "sent" : "failed"
}

function validateExistingLog(
  item: Omit<Incident20260805RecoveryPlanItem, "alreadyRecovered">,
  logs: Incident20260805ExistingLog[]
): boolean {
  const matching = logs.filter(
    (log) => log.clientReference === item.clientReference
  )
  if (matching.length === 0) return false
  if (matching.length !== 1) {
    fail(`Incident job ${item.jobId} matched multiple durable email logs`)
  }

  const existing = matching[0]!
  if (
    existing.id !== item.trackingId ||
    existing.status !== item.outcome ||
    existing.organizationId !== item.job.organizationId ||
    existing.smtpConfigId !== item.job.smtpConfigId ||
    existing.clientType !== INCIDENT_CLIENT_TYPE
  ) {
    fail(`Incident job ${item.jobId} has conflicting durable email state`)
  }
  if (
    item.outcome === "sent"
      ? existing.providerMessageId !== item.providerMessageId
      : existing.providerMessageId !== null
  ) {
    fail(`Incident job ${item.jobId} has conflicting provider state`)
  }
  return true
}

/**
 * Pure, fail-closed incident validation and planning. It accepts only the five
 * immutable completed jobs involved in the 2026-08-05 Freio incident. Existing
 * logs are allowed solely when they are the exact deterministic recovery rows,
 * which makes an interrupted or repeated apply safe.
 */
export function buildIncident20260805RecoveryPlan(
  snapshots: Incident20260805JobSnapshot[],
  existingLogs: Incident20260805ExistingLog[],
  options: Incident20260805RecoveryPlanOptions = {}
): Incident20260805RecoveryPlan {
  if (snapshots.length !== INCIDENT_2026_08_05_JOB_IDS.length) {
    fail("Incident recovery requires exactly five queue jobs")
  }

  const byId = new Map<string, Incident20260805JobSnapshot>()
  for (const snapshot of snapshots) {
    if (
      !snapshot.id ||
      !INCIDENT_2026_08_05_JOB_IDS.includes(
        snapshot.id as Incident20260805JobId
      )
    ) {
      fail("Incident recovery received a missing or unexpected queue job id")
    }
    if (byId.has(snapshot.id)) {
      fail(`Incident recovery received duplicate job ${snapshot.id}`)
    }
    byId.set(snapshot.id, snapshot)
  }

  let organizationId: string | null = null
  let smtpConfigId: string | null = null
  const partialItems: Omit<
    Incident20260805RecoveryPlanItem,
    "alreadyRecovered"
  >[] = []
  const expectedHashes =
    options.expectedClientReferenceSha256 ?? EXPECTED_CLIENT_REFERENCE_SHA256

  for (const jobId of INCIDENT_2026_08_05_JOB_IDS) {
    const snapshot = byId.get(jobId)
    if (!snapshot) fail(`Incident queue job ${jobId} is missing`)
    if (snapshot.state !== "completed") {
      fail(`Incident queue job ${jobId} is not completed`)
    }
    if (
      !Number.isSafeInteger(snapshot.finishedOn) ||
      (snapshot.finishedOn ?? 0) <= 0 ||
      Number.isNaN(new Date(snapshot.finishedOn!).getTime()) ||
      snapshot.finishedOn! < INCIDENT_FINISHED_ON_MIN ||
      snapshot.finishedOn! > INCIDENT_FINISHED_ON_MAX
    ) {
      fail(`Incident queue job ${jobId} is outside the incident time window`)
    }

    const job = validateJobData(snapshot.data, jobId)
    const clientReference = job.clientReference!
    const clientReferenceSha256 = incident20260805Sha256(clientReference)
    if (clientReferenceSha256 !== expectedHashes[jobId]) {
      fail(`Incident queue job ${jobId} has an unexpected clientReference`)
    }

    if (organizationId === null) organizationId = job.organizationId!
    if (smtpConfigId === null) smtpConfigId = job.smtpConfigId
    if (job.organizationId !== organizationId) {
      fail(`Incident queue job ${jobId} has a conflicting organization`)
    }
    if (job.smtpConfigId !== smtpConfigId) {
      fail(`Incident queue job ${jobId} has a conflicting SMTP configuration`)
    }

    const result = parseReturnValue(snapshot.returnvalue, jobId)
    const deduped = result.deduped === true
    const messageId = nonEmptyString(result.messageId)
      ? result.messageId.trim()
      : null
    if (jobId === "1") {
      if (deduped || !messageId) {
        fail("Incident queue job 1 is not the proven SMTP-accepted job")
      }
    } else if (!deduped) {
      fail(`Incident queue job ${jobId} is not a proven false-deduplicated job`)
    }

    partialItems.push({
      jobId,
      clientReference,
      clientReferenceSha256,
      trackingId: incident20260805TrackingId(clientReference),
      job,
      finishedOn: snapshot.finishedOn!,
      outcome: expectedOutcome(jobId),
      providerMessageId: jobId === "1" ? messageId : null,
    })
  }

  const expectedReferences = new Set(
    partialItems.map((item) => item.clientReference)
  )
  if (
    existingLogs.some(
      (log) =>
        log.clientReference === null ||
        !expectedReferences.has(log.clientReference)
    )
  ) {
    fail("Incident recovery received an unrelated durable email log")
  }

  const items = partialItems.map((item) => ({
    ...item,
    alreadyRecovered: validateExistingLog(item, existingLogs),
  }))

  return {
    organizationId: organizationId!,
    smtpConfigId: smtpConfigId!,
    items,
  }
}

export function validateIncident20260805Webhook(
  hooks: Incident20260805WebhookSnapshot[]
): string {
  if (hooks.length !== 1 || hooks[0]?.enabled !== true) {
    fail("Incident recovery requires exactly one enabled organization webhook")
  }
  const hook = hooks[0]!
  if (
    !Array.isArray(hook.events) ||
    !hook.events.includes("email.sent") ||
    !hook.events.includes("email.failed")
  ) {
    fail("Incident webhook must subscribe to email.sent and email.failed")
  }
  return hook.id
}

export function buildIncident20260805FinalizeInput(
  item: Incident20260805RecoveryPlanItem
): FinalizeEmailTerminalInput {
  const commonData = {
    jobId: item.jobId,
    logId: item.trackingId,
    smtpConfigId: item.job.smtpConfigId,
    to: item.job.to.map((recipient) => recipient.email),
    subject: item.job.subject,
    clientReference: item.job.clientReference ?? null,
    clientType: item.job.clientType ?? null,
  }
  const occurredAt = new Date(item.finishedOn)

  if (item.outcome === "sent") {
    return {
      trackingId: item.trackingId,
      jobId: item.jobId,
      job: item.job,
      status: "sent",
      event: "email.sent",
      providerMessageId: item.providerMessageId,
      occurredAt,
      expectedOutboxRows: 1,
      data: {
        ...commonData,
        messageId: item.providerMessageId,
      },
    }
  }

  return {
    trackingId: item.trackingId,
    jobId: item.jobId,
    job: item.job,
    status: "failed",
    event: "email.failed",
    error: INTERNAL_FALSE_DEDUPE_ERROR,
    occurredAt,
    expectedOutboxRows: 1,
    data: {
      ...commonData,
      error: INTERNAL_FALSE_DEDUPE_ERROR,
    },
  }
}

export async function executeIncident20260805Recovery(
  plan: Incident20260805RecoveryPlan,
  finalize: (
    input: FinalizeEmailTerminalInput
  ) => Promise<FinalizeEmailTerminalResult>
): Promise<Incident20260805RecoveryResult[]> {
  const results: Incident20260805RecoveryResult[] = []
  for (const item of plan.items) {
    if (item.alreadyRecovered) {
      results.push({
        jobId: item.jobId,
        clientReferenceSha256: item.clientReferenceSha256,
        trackingId: item.trackingId,
        outcome: item.outcome,
        deduplicated: true,
        outboxRows: 0,
        skipped: true,
      })
      continue
    }
    const result = await finalize(buildIncident20260805FinalizeInput(item))
    if (result.deduplicated || result.outboxRows.length !== 1) {
      fail(
        `Incident job ${item.jobId} returned an unexpected terminal checkpoint; recovery stopped`
      )
    }
    results.push({
      jobId: item.jobId,
      clientReferenceSha256: item.clientReferenceSha256,
      trackingId: item.trackingId,
      outcome: item.outcome,
      deduplicated: result.deduplicated,
      outboxRows: result.outboxRows.length,
      skipped: false,
    })
  }
  return results
}
