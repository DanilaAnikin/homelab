"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

export async function createDomainAction(domain: string) {
  const res = await apiSend<{ id: string }>("POST", "/api/domains", { domain });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/domains");
  redirect(`/dashboard/domains/${res.data!.id}`);
}

export async function deleteDomainAction(id: string) {
  const res = await apiSend("DELETE", `/api/domains/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/domains");
  redirect("/dashboard/domains");
}

export async function verifyDomainAction(id: string) {
  const res = await apiSend("POST", `/api/domains/${id}/verify`);
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/domains/${id}`);
  return { success: true };
}
