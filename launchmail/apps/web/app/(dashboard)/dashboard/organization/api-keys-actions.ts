"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export type ApiKeyRole = "admin" | "writer" | "reader";

export async function createOrgApiKeyAction(input: {
  name: string;
  role: ApiKeyRole;
  smtpConfigId: string | null;
}) {
  const name = input.name.trim();
  if (!name) return { error: "Name is required" };
  // POST /api/api-keys returns the one-time secret as `plaintext`.
  const res = await apiSend<{ id: string; plaintext: string }>("POST", "/api/api-keys", {
    name,
    role: input.role,
    ...(input.smtpConfigId ? { smtpConfigId: input.smtpConfigId } : {}),
  });
  if (!res.ok || !res.data?.plaintext) return { error: res.error ?? "Failed to create key" };
  revalidatePath("/dashboard/organization");
  return { id: res.data.id, token: res.data.plaintext };
}

export async function revokeOrgApiKeyAction(id: string) {
  const res = await apiSend("DELETE", `/api/api-keys/${id}`);
  if (!res.ok) return { error: res.error ?? "Failed to revoke key" };
  revalidatePath("/dashboard/organization");
  return { success: true };
}
