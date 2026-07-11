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
  const session = await apiGet<{ user: SessionUser } | null>(
    "/api/auth/get-session",
  );
  if (!session?.user) return null;
  return session;
}
