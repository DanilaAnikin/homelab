import "server-only";
import { redirect } from "next/navigation";
import { apiFetch } from "./api";
import type { AppRole } from "@workspace/auth/permissions";

export interface OrgContext {
  userId: string;
  userName: string;
  userEmail: string;
  organizationId: string;
  role: AppRole;
}

interface MeResponse {
  organizationId: string;
  role: AppRole;
}
interface SessionResponse {
  user: { id: string; name: string; email: string };
}

/**
 * Resolve a JSON response, distinguishing the failure classes so callers can
 * react correctly:
 *  - 401/403 -> "unauthorized": the caller should send the user to /login.
 *  - 404 -> null data (genuine "no data", e.g. user not in any org).
 *  - 5xx / network / malformed body -> throws, so the error boundary renders
 *    rather than bouncing an authenticated user to /login (no login loop).
 */
async function loadJson<T>(
  path: string,
): Promise<{ unauthorized: true } | { unauthorized: false; data: T | null }> {
  let res: Response;
  try {
    res = await apiFetch(path);
  } catch (e) {
    throw new Error(`API request to ${path} failed: ${(e as Error).message}`);
  }
  if (res.status === 401 || res.status === 403) {
    return { unauthorized: true };
  }
  if (res.status === 404) return { unauthorized: false, data: null };
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `API request to ${path} failed (${res.status})${text ? `: ${text}` : ""}`,
    );
  }
  try {
    return { unauthorized: false, data: (await res.json()) as T };
  } catch (e) {
    throw new Error(
      `API response from ${path} was not valid JSON: ${(e as Error).message}`,
    );
  }
}

export async function getOrgContext(): Promise<OrgContext | null> {
  const [me, session] = await Promise.all([
    loadJson<MeResponse>("/api/me"),
    loadJson<SessionResponse>("/api/auth/get-session"),
  ]);
  // Unauthenticated / forbidden: there is no valid session to build a context
  // from. requireOrg() turns this into a /login redirect.
  if (me.unauthorized || session.unauthorized) return null;
  if (!me.data || !session.data?.user) return null;
  return {
    userId: session.data.user.id,
    userName: session.data.user.name,
    userEmail: session.data.user.email,
    organizationId: me.data.organizationId,
    role: me.data.role,
  };
}

export async function requireOrg(): Promise<OrgContext> {
  // getOrgContext throws on 5xx/network (handled by the error boundary) and
  // returns null only for unauthenticated/forbidden/no-org states.
  const ctx = await getOrgContext();
  if (!ctx) redirect("/login");
  return ctx;
}
