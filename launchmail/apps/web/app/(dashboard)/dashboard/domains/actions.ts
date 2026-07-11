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

export interface DeliverabilityReport {
  domain: string;
  egressIp?: string;
  heloHostname?: string;
  checks: { key: string; label: string; ok: boolean; detail: string }[];
  records: { type: string; name: string; value: string; purpose: string }[];
  passed: number;
  total: number;
  ready: boolean;
}

export async function checkDeliverabilityAction(id: string) {
  const res = await apiSend<DeliverabilityReport>(
    "GET",
    `/api/domains/${id}/deliverability`,
  );
  if (!res.ok) return { error: res.error };
  return { report: res.data! };
}
