import "server-only";
import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";

const API_URL = process.env.API_URL ?? "http://localhost:5000";

export async function bearerToken(): Promise<string | undefined> {
  const raw = (await cookies()).get("lm_token")?.value;
  // The token is stored URL-encoded (setToken uses encodeURIComponent). Decode
  // it so the base64 signature (+, /, =) is intact when sent as a Bearer token;
  // otherwise the API rejects it and the session reads empty. Safe to always
  // run: an already-decoded value has no "%" escapes to change.
  return raw ? decodeURIComponent(raw) : undefined;
}

export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const token = await bearerToken();
  // Server components call the API over the private Docker network, bypassing
  // the public reverse proxy. Preserve the client-IP header that Cloudflare
  // overwrote at ingress so Better Auth can apply its per-IP rate limit to
  // these requests too. Never derive this value from X-Forwarded-For.
  const cloudflareClientIp = (await headers()).get("cf-connecting-ip");
  return fetch(`${API_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...((init?.headers as Record<string, string>) ?? {}),
      ...(cloudflareClientIp
        ? { "cf-connecting-ip": cloudflareClientIp }
        : {}),
    },
  });
}

/**
 * Fetch JSON from the API for read-only page data.
 *
 * Error handling distinguishes the failure classes so the UI never shows a
 * fake-empty / zero state for a real backend outage:
 *  - 404 (genuine "no data") -> returns null so callers can render an
 *    empty/not-found state.
 *  - 401 (unauthenticated) -> redirects to /login.
 *  - everything else (5xx, network errors, malformed JSON) -> throws, so the
 *    nearest Next error boundary (error.tsx) renders instead of empty data.
 */
export async function apiGet<T>(path: string): Promise<T | null> {
  let res: Response;
  try {
    res = await apiFetch(path);
  } catch (e) {
    throw new Error(`API request to ${path} failed: ${(e as Error).message}`);
  }
  if (res.status === 404) return null;
  if (res.status === 401) redirect("/login");
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `API request to ${path} failed (${res.status})${text ? `: ${text}` : ""}`,
    );
  }
  try {
    return (await res.json()) as T;
  } catch (e) {
    throw new Error(
      `API response from ${path} was not valid JSON: ${(e as Error).message}`,
    );
  }
}

export interface ApiResult<T> {
  ok: boolean;
  status: number;
  data: T | null;
  error?: string;
}

export async function apiSend<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  try {
    const res = await apiFetch(path, {
      method,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const data = text ? safeJson(text) : null;
    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        data: null,
        error:
          (data && typeof data === "object" && "error" in data
            ? String((data as { error: unknown }).error)
            : text) || `Request failed (${res.status})`,
      };
    }
    return { ok: true, status: res.status, data: data as T };
  } catch (e) {
    return { ok: false, status: 0, data: null, error: (e as Error).message };
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
