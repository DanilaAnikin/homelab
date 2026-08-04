"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";
import type { WebhookEvent as Event } from "@workspace/db/schemas";

export async function createWebhookAction(url: string, events: Event[]) {
  const res = await apiSend("POST", "/api/webhooks", { url, events });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/webhooks");
  return { success: true };
}

export async function updateWebhookAction(
  id: string,
  input: { url?: string; events?: Event[]; enabled?: boolean },
) {
  const res = await apiSend("PATCH", `/api/webhooks/${id}`, input);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/webhooks");
  return { success: true };
}

export async function deleteWebhookAction(id: string) {
  const res = await apiSend("DELETE", `/api/webhooks/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/webhooks");
  return { success: true };
}

export async function pingWebhookAction(id: string) {
  const res = await apiSend<{ lastStatus: string }>(
    "POST",
    `/api/webhooks/${id}/ping`,
  );
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/webhooks");
  return { success: true, status: res.data?.lastStatus };
}
