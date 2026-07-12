export interface LaunchMailClientOptions {
  baseUrl: string;
  apiKey: string;
}

export interface Identity {
  organizationId: string;
  role: "admin" | "writer" | "reader";
  authKind: string;
}

export interface SendEmailInput {
  from?: string;
  replyTo?: string;
  to: { email: string; name?: string }[];
  cc?: { email: string; name?: string }[];
  bcc?: { email: string; name?: string }[];
  subject: string;
  html?: string;
  text?: string;
  /**
   * ISO 8601 timestamp. MUST be UTC ending in "Z" (e.g. "2026-07-20T10:00:00Z")
   * — the server validates with Zod `.datetime()`, which rejects timezone
   * offsets like "+02:00" with HTTP 400. A future timestamp => scheduled delivery.
   */
  sendAt?: string;
}

export interface SendEmailResult {
  id: string;
  status: "queued" | "scheduled";
  smtpConfigId: string;
  scheduledAt: string | null;
  createdAt: string;
}

export interface IncomingEmailSummary {
  id: string;
  smtpConfigId: string;
  fromAddress: string;
  fromName: string | null;
  subject: string | null;
  snippet: string | null;
  seen: boolean;
  starred: boolean;
  archived: boolean;
  hasAttachments: boolean;
  repliedAt: string | null; // ISO string over the wire (Date server-side)
  receivedAt: string; // ISO string over the wire (Date server-side)
}

export interface ListIncomingEmailsOptions {
  limit?: number; // default 50, server caps at 100
  folder?: "inbox" | "archived" | "starred" | "all"; // default "inbox"
  smtpConfigId?: string;
  q?: string;
  before?: string; // ISO cursor for pagination
}

export interface CreateSmtpConfigInput {
  name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  fromAddress: string;
  fromName?: string;
}

export interface CreateFormInput {
  name: string;
  templateKey: string;
  recipients: string[];
  subject?: string;
}

export interface CreateApiKeyInput {
  name: string;
  role?: "admin" | "writer" | "reader";
  smtpConfigId?: string;
}

export class LaunchMailError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "LaunchMailError";
    this.status = status;
  }
}

export class LaunchMailClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;

  constructor(options: LaunchMailClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.apiKey = options.apiKey;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options?: { okStatuses?: number[] },
  ): Promise<T> {
    const res = await fetch(`${this.baseUrl}/api${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const data = text ? safeJson(text) : null;
    // Some endpoints (e.g. SMTP test) use a non-2xx status to convey a
    // structured result rather than a transport/auth error. Callers can opt
    // those statuses in so the parsed body is returned instead of thrown.
    const accepted = res.ok || (options?.okStatuses?.includes(res.status) ?? false);
    if (!accepted) {
      const message =
        (data && typeof data === "object" && "error" in data
          ? String((data as { error: unknown }).error)
          : text) || `Request failed (${res.status})`;
      throw new LaunchMailError(res.status, message);
    }
    return data as T;
  }

  whoami() {
    return this.request<Identity>("GET", "/me");
  }

  // Email
  sendEmail(input: SendEmailInput) {
    return this.request<SendEmailResult>("POST", "/mail/send", input);
  }
  /**
   * Pull received messages (e.g. replies) for the token's organization.
   * Returns a bare array of summaries, newest first. `inbox:read` is granted to
   * every token role, so any `lm_…` token can call this.
   */
  listIncomingEmails(opts: ListIncomingEmailsOptions = {}) {
    const qs = new URLSearchParams();
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    if (opts.folder) qs.set("folder", opts.folder);
    if (opts.smtpConfigId) qs.set("smtpConfigId", opts.smtpConfigId);
    if (opts.q) qs.set("q", opts.q);
    if (opts.before) qs.set("before", opts.before);
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.request<IncomingEmailSummary[]>(
      "GET",
      `/incoming-emails${suffix}`,
    );
  }
  listLogs(limit = 100) {
    return this.request<unknown[]>("GET", `/logs?limit=${limit}`);
  }

  // SMTP configs
  listSmtpConfigs() {
    return this.request<unknown[]>("GET", "/smtp-configs");
  }
  createSmtpConfig(input: CreateSmtpConfigInput) {
    return this.request<unknown>("POST", "/smtp-configs", input);
  }
  testSmtpConnection(id: string) {
    // A reachable-but-failed connection is reported as HTTP 422 with
    // { success: false, error }. Treat that as a normal result rather than a
    // thrown error so callers can surface the failure cleanly. Genuine
    // transport/auth/not-found errors (401/404/5xx) still throw.
    return this.request<{ success: boolean; message?: string; error?: string }>(
      "POST",
      `/smtp-configs/${id}/test`,
      undefined,
      { okStatuses: [422] },
    );
  }
  deleteSmtpConfig(id: string) {
    return this.request<{ success: boolean }>(
      "DELETE",
      `/smtp-configs/${id}`,
    );
  }

  // API keys
  listApiKeys() {
    return this.request<unknown[]>("GET", "/api-keys");
  }
  createApiKey(input: CreateApiKeyInput) {
    return this.request<{ id: string; plaintext: string }>(
      "POST",
      "/api-keys",
      input,
    );
  }
  revokeApiKey(id: string) {
    return this.request<{ success: boolean }>("DELETE", `/api-keys/${id}`);
  }

  // Forms
  listForms() {
    return this.request<unknown[]>("GET", "/forms");
  }
  createForm(input: CreateFormInput) {
    return this.request<{ id: string; endpointToken: string }>(
      "POST",
      "/forms",
      input,
    );
  }
  getForm(id: string) {
    return this.request<unknown>("GET", `/forms/${id}`);
  }
  deleteForm(id: string) {
    return this.request<{ success: boolean }>("DELETE", `/forms/${id}`);
  }
  listSubmissions(id: string) {
    return this.request<unknown[]>("GET", `/forms/${id}/submissions`);
  }
}

// ── Webhooks ────────────────────────────────────────────────────────────────

export type LaunchMailWebhookEvent =
  | "email.sent"
  | "email.failed"
  | "email.bounced"
  | "form.submission"
  | "incoming.received"
  | "ping";

export interface LaunchMailWebhookPayload<T = unknown> {
  event: LaunchMailWebhookEvent;
  createdAt: string;
  data: T;
}

export interface EmailSentData {
  to: string[];
  subject: string;
  messageId: string;
}
export interface EmailFailedData {
  to: string[];
  subject: string;
  error: string;
}
export interface IncomingReceivedData {
  id: string;
  smtpConfigId: string;
  from: string;
  fromName: string | null;
  subject: string | null;
  receivedAt: string;
}

/**
 * Verify an `X-LaunchMail-Signature` header against the RAW request body.
 * Header format: `sha256=<hex HMAC-SHA256(rawBody, secret)>`.
 * Universal (Web Crypto): works on Node 20+, Vercel Edge, Bun, browsers.
 * (Node 18 would need `--experimental-global-webcrypto`; this SDK targets Node 20+.)
 *
 * `secret` must be the FULL webhook secret shown in LaunchMail, including the
 * `whsec_` prefix — the server signs with it verbatim. Pass the EXACT raw body
 * string you received; do NOT re-stringify the parsed JSON.
 */
export async function verifyWebhookSignature(
  rawBody: string,
  signatureHeader: string | null | undefined,
  secret: string,
): Promise<boolean> {
  if (!secret || !signatureHeader) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(rawBody));
  const expected =
    "sha256=" +
    [...new Uint8Array(sig)]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  // constant-time-ish compare over fixed-length "sha256="+64-hex strings
  if (expected.length !== signatureHeader.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ signatureHeader.charCodeAt(i);
  }
  return diff === 0;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
