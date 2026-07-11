import { randomBytes, createHmac } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import { isIP } from "node:net";
import { request as httpRequest, type IncomingMessage } from "node:http";
import { request as httpsRequest } from "node:https";
import { db } from "@workspace/db";
import { webhooks } from "@workspace/db/schemas";
import type { Webhook, WebhookEvent } from "@workspace/db/schemas";
import { and, desc, eq } from "drizzle-orm";

export type { Webhook, WebhookEvent };

// --- SSRF protection -------------------------------------------------------
// Webhook URLs are attacker-controlled, so every outbound request must be
// proven to target a public address. We resolve the host, reject any
// private/loopback/link-local/multicast/metadata IP, and then PIN the
// connection to the validated IPs via a custom DNS lookup so a rebinding
// attack (DNS answer flips to 127.0.0.1 between validation and connect)
// cannot redirect us onto the internal network. SNI/Host/cert validation is
// preserved because we keep the original hostname.

class SsrfError extends Error {}

function ipv4ToParts(ip: string): number[] | null {
  const parts = ip.split(".");
  if (parts.length !== 4) return null;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return null;
  return nums;
}

function isPrivateIp(ip: string): boolean {
  const family = isIP(ip);

  if (family === 4) {
    const p = ipv4ToParts(ip);
    if (!p) return true; // unparseable → treat as unsafe
    const [a, b] = p as [number, number, number, number];
    if (a === 0) return true; // 0.0.0.0/8
    if (a === 10) return true; // 10.0.0.0/8
    if (a === 127) return true; // 127.0.0.0/8 loopback
    if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local + metadata
    if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12
    if (a === 192 && b === 168) return true; // 192.168.0.0/16
    if (a === 100 && b >= 64 && b <= 127) return true; // 100.64.0.0/10 CGNAT
    if (a >= 224) return true; // 224.0.0.0/4 multicast + 240/4 reserved
    return false;
  }

  if (family === 6) {
    const lower = ip.toLowerCase();
    // IPv4-mapped (::ffff:a.b.c.d) → validate the embedded v4 address.
    const mapped = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (mapped) return isPrivateIp(mapped[1]!);
    if (lower === "::1" || lower === "::") return true; // loopback / unspecified
    if (lower.startsWith("fe80")) return true; // link-local
    if (lower.startsWith("fc") || lower.startsWith("fd")) return true; // unique-local fc00::/7
    if (lower.startsWith("ff")) return true; // multicast
    // IPv4-compatible / NAT64 well-known prefix and similar embeddings.
    if (lower.startsWith("64:ff9b:")) return true;
    return false;
  }

  // Not a recognizable IP literal → unsafe.
  return true;
}

interface PinnedTarget {
  parsed: URL;
  addresses: { address: string; family: number }[];
}

// Resolve + validate. Throws SsrfError when the URL scheme is not http(s) or
// when any resolved address is non-public. Returns the resolved addresses so
// the connection can be pinned to them.
async function resolveSafeTarget(rawUrl: string): Promise<PinnedTarget> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new SsrfError("Invalid webhook URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new SsrfError("Webhook URL must use http(s)");
  }

  const host = parsed.hostname;

  // A bare IP literal in the URL still has to pass the public-IP check.
  if (isIP(host)) {
    if (isPrivateIp(host)) {
      throw new SsrfError(`Webhook host resolves to a blocked address: ${host}`);
    }
    return { parsed, addresses: [{ address: host, family: isIP(host) }] };
  }

  const resolved = await dnsLookup(host, { all: true }).catch(() => {
    throw new SsrfError(`Could not resolve webhook host: ${host}`);
  });
  if (resolved.length === 0) {
    throw new SsrfError(`Could not resolve webhook host: ${host}`);
  }
  for (const { address } of resolved) {
    if (isPrivateIp(address)) {
      throw new SsrfError(
        `Webhook host resolves to a blocked address: ${address}`,
      );
    }
  }
  return { parsed, addresses: resolved };
}

export interface SafeFetchResult {
  status: number;
}

// SSRF-safe POST used for all webhook dispatch. Validates + pins the target,
// then issues the request against the original hostname (preserving TLS SNI
// and Host) while restricting the socket to the pre-validated IPs.
export async function ssrfSafePost(
  rawUrl: string,
  body: string,
  headers: Record<string, string>,
  timeoutMs = 10000,
): Promise<SafeFetchResult> {
  const { parsed, addresses } = await resolveSafeTarget(rawUrl);
  const allowed = new Set(addresses.map((a) => a.address));
  const requestFn = parsed.protocol === "https:" ? httpsRequest : httpRequest;

  return new Promise<SafeFetchResult>((resolve, reject) => {
    const req = requestFn(
      {
        protocol: parsed.protocol,
        hostname: parsed.hostname,
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
        lookup: (hostname, _opts, cb) => {
          dnsLookup(hostname, { all: true }).then(
            (results) => {
              const safe = results.find(
                (r) => allowed.has(r.address) && !isPrivateIp(r.address),
              );
              if (!safe) {
                cb(
                  new SsrfError(
                    `Webhook host re-resolved to a blocked address`,
                  ),
                  "",
                  4,
                );
                return;
              }
              cb(null, safe.address, safe.family);
            },
            (err) => cb(err as NodeJS.ErrnoException, "", 4),
          );
        },
      },
      (res: IncomingMessage) => {
        // Drain and discard the body; we only care about the status.
        res.resume();
        res.on("end", () => resolve({ status: res.statusCode ?? 0 }));
        res.on("error", reject);
      },
    );
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// Validate a webhook URL at create/update time, surfacing a clear error.
export async function assertSafeWebhookUrl(rawUrl: string): Promise<void> {
  await resolveSafeTarget(rawUrl);
}

export interface CreateWebhookInput {
  url: string;
  events: WebhookEvent[];
}
export interface UpdateWebhookInput {
  url?: string;
  events?: WebhookEvent[];
  enabled?: boolean;
}

export async function createWebhook(
  organizationId: string,
  input: CreateWebhookInput,
): Promise<Webhook> {
  // Reject SSRF targets before persisting the webhook.
  await assertSafeWebhookUrl(input.url);
  const secret = `whsec_${randomBytes(24).toString("hex")}`;
  const [row] = await db
    .insert(webhooks)
    .values({
      organizationId,
      url: input.url,
      events: input.events,
      secret,
    })
    .returning();
  return row!;
}

export async function listWebhooks(
  organizationId: string,
): Promise<Webhook[]> {
  return db
    .select()
    .from(webhooks)
    .where(eq(webhooks.organizationId, organizationId))
    .orderBy(desc(webhooks.createdAt));
}

export async function getWebhook(
  id: string,
  organizationId: string,
): Promise<Webhook | null> {
  const [row] = await db
    .select()
    .from(webhooks)
    .where(and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId)));
  return row ?? null;
}

export async function updateWebhook(
  id: string,
  organizationId: string,
  input: UpdateWebhookInput,
): Promise<Webhook | null> {
  const updates: Partial<typeof webhooks.$inferInsert> = {};
  if (input.url !== undefined) {
    // Re-validate the new target before storing it.
    await assertSafeWebhookUrl(input.url);
    updates.url = input.url;
  }
  if (input.events !== undefined) updates.events = input.events;
  if (input.enabled !== undefined) updates.enabled = input.enabled;
  const [row] = await db
    .update(webhooks)
    .set(updates)
    .where(and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId)))
    .returning();
  return row ?? null;
}

export async function deleteWebhook(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(webhooks)
    .where(and(eq(webhooks.id, id), eq(webhooks.organizationId, organizationId)))
    .returning();
  return rows.length > 0;
}

async function deliver(
  hook: Webhook,
  event: string,
  data: unknown,
): Promise<void> {
  const body = JSON.stringify({
    event,
    createdAt: new Date().toISOString(),
    data,
  });
  const signature = createHmac("sha256", hook.secret)
    .update(body)
    .digest("hex");
  let status = "error";
  try {
    const res = await ssrfSafePost(
      hook.url,
      body,
      {
        "Content-Type": "application/json",
        "X-LaunchMail-Event": event,
        "X-LaunchMail-Signature": `sha256=${signature}`,
      },
      10000,
    );
    status = `${res.status}`;
  } catch (e) {
    const err = e as Error;
    if (err instanceof SsrfError) status = "blocked";
    else if (err.message === "timeout") status = "timeout";
    else status = "error";
  }
  await db
    .update(webhooks)
    .set({ lastStatus: status, lastDeliveredAt: new Date() })
    .where(eq(webhooks.id, hook.id))
    .catch(() => undefined);
}

export async function dispatchEvent(
  organizationId: string,
  event: WebhookEvent,
  data: unknown,
): Promise<void> {
  const hooks = await db
    .select()
    .from(webhooks)
    .where(
      and(
        eq(webhooks.organizationId, organizationId),
        eq(webhooks.enabled, true),
      ),
    );
  const matching = hooks.filter((h) => h.events.includes(event));
  await Promise.allSettled(matching.map((h) => deliver(h, event, data)));
}

export async function pingWebhook(
  id: string,
  organizationId: string,
): Promise<Webhook | null> {
  const hook = await getWebhook(id, organizationId);
  if (!hook) return null;
  await deliver(hook, "ping", { message: "Test event from LaunchMail" });
  return getWebhook(id, organizationId);
}
