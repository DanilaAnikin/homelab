"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export async function retryJobAction(id: string) {
  const res = await apiSend("POST", `/api/failed/${id}/retry`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/analytics");
  return { success: true };
}
