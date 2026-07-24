import "server-only";
import { apiGet } from "./api";

export interface SessionUser {
  id: string;
  name: string;
  email: string;
  emailVerified?: boolean;
  createdAt?: string;
}

export async function getSession(): Promise<{ user: SessionUser } | null> {
  // Retry a couple of times: a transient backend/DB hiccup can return an empty
  // session for a request that is actually authenticated, which would otherwise
  // bounce the user straight back to /login. Genuine logged-out reads just cost
  // a couple hundred ms before redirecting.
  for (let attempt = 0; attempt < 3; attempt++) {
    const session = await apiGet<{ user: SessionUser } | null>(
      "/api/auth/get-session",
    );
    if (session?.user) return session;
    if (attempt < 2) await new Promise((r) => setTimeout(r, 150));
  }
  return null;
}
