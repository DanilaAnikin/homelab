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
    return this.request<{ id: string; status: string }>(
      "POST",
      "/mail/send",
      input,
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

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
