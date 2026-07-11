"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export async function addSuppressionAction(email: string) {
  const res = await apiSend("POST", "/api/suppressions", { email });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/suppressions");
  return { success: true };
}

export async function removeSuppressionAction(id: string) {
  const res = await apiSend("DELETE", `/api/suppressions/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/suppressions");
  return { success: true };
}
