"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

export async function createAudienceAction(name: string) {
  const res = await apiSend<{ id: string }>("POST", "/api/audiences", { name });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/audiences");
  redirect(`/dashboard/audiences/${res.data!.id}`);
}

export async function deleteAudienceAction(id: string) {
  const res = await apiSend("DELETE", `/api/audiences/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/audiences");
  redirect("/dashboard/audiences");
}

export async function addContactsAction(
  audienceId: string,
  contacts: { email: string; firstName?: string; lastName?: string }[],
) {
  const res = await apiSend<{ added: number }>(
    "POST",
    `/api/audiences/${audienceId}/contacts`,
    { contacts },
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/audiences/${audienceId}`);
  return { added: res.data?.added ?? 0 };
}

export async function removeContactAction(audienceId: string, contactId: string) {
  const res = await apiSend(
    "DELETE",
    `/api/audiences/${audienceId}/contacts/${contactId}`,
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/audiences/${audienceId}`);
  return { success: true };
}

export async function sendBroadcastAction(input: {
  audienceId: string;
  templateId: string;
  smtpConfigId?: string;
  name: string;
  subject: string;
}) {
  const res = await apiSend<{ sentCount: number; failedCount?: number }>(
    "POST",
    "/api/broadcasts",
    input,
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/audiences/${input.audienceId}`);
  return {
    sentCount: res.data?.sentCount ?? 0,
    failedCount: res.data?.failedCount ?? 0,
  };
}
