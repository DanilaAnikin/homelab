import { randomBytes, createHmac } from "node:crypto"
import type { LookupAddress, LookupAllOptions, LookupOptions } from "node:dns"
import { lookup as dnsLookup } from "node:dns/promises"
import { isIP, type LookupFunction } from "node:net"
import { request as httpRequest, type IncomingMessage } from "node:http"
import { request as httpsRequest } from "node:https"
import ipaddr from "ipaddr.js"
import { db } from "@workspace/db"
import { webhooks } from "@workspace/db/schemas"
import type { Webhook, WebhookEvent } from "@workspace/db/schemas"
import { and, desc, eq } from "drizzle-orm"
import {
  persistWebhookEvent,
  relayWebhookOutboxRows,
  type PersistWebhookEventOptions,
} from "./webhook-outbox"

export type { Webhook, WebhookEvent }

// --- SSRF protection -------------------------------------------------------
// Webhook URLs are attacker-controlled, so every outbound request must be
// proven to target a public address. We resolve the host, reject any
// private/loopback/link-local/multicast/metadata IP, and then PIN the
// connection to the validated IPs via a custom DNS lookup so a rebinding
// attack (DNS answer flips to 127.0.0.1 between validation and connect)
// cannot redirect us onto the internal network. SNI/Host/cert validation is
// preserved because we keep the original hostname.

export class SsrfError extends Error {}

export function isPrivateIp(ip: string): boolean {
  try {
    // process() converts every IPv4-mapped spelling (including hexadecimal
    // ::ffff:7f00:1) to IPv4 before classification. range() covers complete
    // CIDRs such as IPv6 fe80::/10 rather than fragile string prefixes.
    return ipaddr.process(ip).range() !== "unicast"
  } catch {
    // Not a recognizable IP literal → unsafe.
    return true
  }
}

interface PinnedTarget {
  parsed: URL
  addresses: { address: string; family: number }[]
}

function hostnameWithoutIpv6Brackets(hostname: string): string {
  return hostname.startsWith("[") && hostname.endsWith("]")
    ? hostname.slice(1, -1)
    : hostname
}

export type PinnedLookupResolver = (
  hostname: string,
  options: LookupAllOptions
) => Promise<LookupAddress[]>

const defaultPinnedLookupResolver: PinnedLookupResolver = (hostname, options) =>
  dnsLookup(hostname, options)

function requestedFamily(family: LookupOptions["family"]): number | undefined {
  if (family === "IPv4") return 4
  if (family === "IPv6") return 6
  return family && family !== 0 ? family : undefined
}

/**
 * Build Node's custom lookup callback around the addresses validated during
 * the first DNS pass. Every later answer must still be public and overlap the
 * pinned set. Node 20 requests `all: true` when auto family selection is in
 * use, so that branch must return LookupAddress[] rather than `(address,
 * family)`.
 */
export function createPinnedLookup(
  pinnedAddresses: readonly LookupAddress[],
  resolveAll: PinnedLookupResolver = defaultPinnedLookupResolver
): LookupFunction {
  const pinned = new Map<string, number>()
  for (const candidate of pinnedAddresses) {
    const actualFamily = isIP(candidate.address)
    if (
      (actualFamily === 4 || actualFamily === 6) &&
      candidate.family === actualFamily &&
      !isPrivateIp(candidate.address)
    ) {
      pinned.set(candidate.address, actualFamily)
    }
  }

  return (hostname, options, callback) => {
    const resolverOptions: LookupAllOptions = {
      all: true,
      family: options.family,
      hints: options.hints,
      verbatim: options.verbatim,
    }
    const family = requestedFamily(options.family)

    resolveAll(hostname, resolverOptions).then(
      (results) => {
        const seen = new Set<string>()
        const safe: LookupAddress[] = []
        for (const result of results) {
          const actualFamily = isIP(result.address)
          if (
            (actualFamily !== 4 && actualFamily !== 6) ||
            result.family !== actualFamily ||
            pinned.get(result.address) !== actualFamily ||
            isPrivateIp(result.address) ||
            (family !== undefined && actualFamily !== family) ||
            seen.has(result.address)
          ) {
            continue
          }
          seen.add(result.address)
          safe.push({ address: result.address, family: actualFamily })
        }

        if (safe.length === 0) {
          const error = new SsrfError(
            "Webhook host re-resolved without a safe pinned address"
          )
          if (options.all === true) callback(error, [])
          else callback(error, "", 0)
          return
        }

        if (options.all === true) {
          callback(null, safe)
          return
        }
        const selected = safe[0]!
        callback(null, selected.address, selected.family)
      },
      (error: unknown) => {
        const lookupError =
          error instanceof Error
            ? (error as NodeJS.ErrnoException)
            : new Error("Webhook DNS lookup failed")
        if (options.all === true) callback(lookupError, [])
        else callback(lookupError, "", 0)
      }
    )
  }
}

// Resolve + validate. Throws SsrfError when the URL scheme is not http(s) or
// when any resolved address is non-public. Returns the resolved addresses so
// the connection can be pinned to them.
async function resolveSafeTarget(rawUrl: string): Promise<PinnedTarget> {
  let parsed: URL
  try {
    parsed = new URL(rawUrl)
  } catch {
    throw new SsrfError("Invalid webhook URL")
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new SsrfError("Webhook URL must use http(s)")
  }

  const host = hostnameWithoutIpv6Brackets(parsed.hostname)

  // A bare IP literal in the URL still has to pass the public-IP check.
  if (isIP(host)) {
    if (isPrivateIp(host)) {
      throw new SsrfError(`Webhook host resolves to a blocked address: ${host}`)
    }
    return { parsed, addresses: [{ address: host, family: isIP(host) }] }
  }

  const resolved = await dnsLookup(host, { all: true }).catch(() => {
    throw new SsrfError(`Could not resolve webhook host: ${host}`)
  })
  if (resolved.length === 0) {
    throw new SsrfError(`Could not resolve webhook host: ${host}`)
  }
  for (const { address } of resolved) {
    if (isPrivateIp(address)) {
      throw new SsrfError(
        `Webhook host resolves to a blocked address: ${address}`
      )
    }
  }
  return { parsed, addresses: resolved }
}

export interface SafeFetchResult {
  status: number
}

// SSRF-safe POST used for all webhook dispatch. Validates + pins the target,
// then issues the request against the original hostname (preserving TLS SNI
// and Host) while restricting the socket to the pre-validated IPs.
export async function ssrfSafePost(
  rawUrl: string,
  body: string,
  headers: Record<string, string>,
  timeoutMs = 10000
): Promise<SafeFetchResult> {
  const { parsed, addresses } = await resolveSafeTarget(rawUrl)
  const requestFn = parsed.protocol === "https:" ? httpsRequest : httpRequest

  return new Promise<SafeFetchResult>((resolve, reject) => {
    const req = requestFn(
      {
        protocol: parsed.protocol,
        hostname: hostnameWithoutIpv6Brackets(parsed.hostname),
        port: parsed.port || (parsed.protocol === "https:" ? 443 : 80),
        path: `${parsed.pathname}${parsed.search}`,
        method: "POST",
        headers: {
          ...headers,
          Host: parsed.host,
          "Content-Length": Buffer.byteLength(body).toString(),
        },
        timeout: timeoutMs,
        // Pin the connection to the validated IPs: the lookup only ever yields
        // an address we already cleared, defeating DNS rebinding.
        lookup: createPinnedLookup(addresses),
      },
      (res: IncomingMessage) => {
        // Drain and discard the body; we only care about the status.
        res.resume()
        res.on("end", () => resolve({ status: res.statusCode ?? 0 }))
        res.on("error", reject)
      }
    )
    req.on("timeout", () => req.destroy(new Error("timeout")))
    req.on("error", reject)
    req.write(body)
    req.end()
  })
}

// Validate a webhook URL at create/update time, surfacing a clear error.
export async function assertSafeWebhookUrl(rawUrl: string): Promise<void> {
  await resolveSafeTarget(rawUrl)
}

export interface CreateWebhookInput {
  url: string
  events: WebhookEvent[]
}
export interface UpdateWebhookInput {
  url?: string
  events?: WebhookEvent[]
  enabled?: boolean
}

export async function createWebhook(
  organizationId: string,
  input: CreateWebhookInput
): Promise<Webhook> {
  // Reject SSRF targets before persisting the webhook.
  await assertSafeWebhookUrl(input.url)
  const secret = `whsec_${randomBytes(24).toString("hex")}`
  const [row] = await db
    .insert(webhooks)
    .values({
      organizationId,
      url: input.url,
      events: input.events,
      secret,
    })
    .returning()
  return row!
}

export async function listWebhooks(organizationId: string): Promise<Webhook[]> {
  return db
    .select()
    .from(webhooks)
    .where(eq(webhooks.organizationId, organizationId))
    .orderBy(desc(webhooks.createdAt))
}

export async function getWebhook(
  id: string,
  organizationId: string
): Promise<Webhook | null> {
  const [row] = await db
    .select()
    .from(webhooks)
    .where(
      and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId))
    )
  return row ?? null
}

export async function updateWebhook(
  id: string,
  organizationId: string,
  input: UpdateWebhookInput
): Promise<Webhook | null> {
  const updates: Partial<typeof webhooks.$inferInsert> = {}
  if (input.url !== undefined) {
    // Re-validate the new target before storing it.
    await assertSafeWebhookUrl(input.url)
    updates.url = input.url
  }
  if (input.events !== undefined) updates.events = input.events
  if (input.enabled !== undefined) updates.enabled = input.enabled
  const [row] = await db
    .update(webhooks)
    .set(updates)
    .where(
      and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId))
    )
    .returning()
  return row ?? null
}

export async function deleteWebhook(
  id: string,
  organizationId: string
): Promise<boolean> {
  const rows = await db
    .delete(webhooks)
    .where(
      and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId))
    )
    .returning()
  return rows.length > 0
}

async function deliver(
  hook: Webhook,
  event: string,
  data: unknown
): Promise<void> {
  const body = JSON.stringify({
    event,
    createdAt: new Date().toISOString(),
    data,
  })
  const signature = createHmac("sha256", hook.secret).update(body).digest("hex")
  let status = "error"
  try {
    const res = await ssrfSafePost(
      hook.url,
      body,
      {
        "Content-Type": "application/json",
        "X-LaunchMail-Event": event,
        "X-LaunchMail-Signature": `sha256=${signature}`,
      },
      10000
    )
    status = `${res.status}`
  } catch (e) {
    const err = e as Error
    if (err instanceof SsrfError) status = "blocked"
    else if (err.message === "timeout") status = "timeout"
    else status = "error"
  }
  await db
    .update(webhooks)
    .set({ lastStatus: status, lastDeliveredAt: new Date() })
    .where(eq(webhooks.id, hook.id))
    .catch(() => undefined)
}

export async function dispatchEvent(
  organizationId: string,
  event: WebhookEvent,
  data: unknown,
  options: PersistWebhookEventOptions = {}
): Promise<void> {
  const rows = await persistWebhookEvent(
    db,
    organizationId,
    event,
    data,
    options
  )
  await relayWebhookOutboxRows(rows).catch((error) => {
    console.error(
      `[webhooks] Event persisted to outbox but immediate relay failed: ${
        error instanceof Error ? error.message : String(error)
      }`
    )
  })
}

export async function pingWebhook(
  id: string,
  organizationId: string
): Promise<Webhook | null> {
  const hook = await getWebhook(id, organizationId)
  if (!hook) return null
  await deliver(hook, "ping", { message: "Test event from LaunchMail" })
  return getWebhook(id, organizationId)
}
