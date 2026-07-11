"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import type { EmailBlock } from "@workspace/templates";

export interface TemplatePayload {
  name: string;
  mode: "html" | "blocks";
  subject?: string | null;
  html?: string | null;
  blocks?: EmailBlock[] | null;
  accent?: string | null;
  theme?: "light" | "dark" | null;
}

export async function createTemplateAction(input: TemplatePayload) {
  const res = await apiSend<{ id: string }>("POST", "/api/templates", input);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/templates");
  redirect(`/dashboard/templates/${res.data!.id}`);
}

export async function updateTemplateAction(
  id: string,
  input: TemplatePayload,
) {
  const res = await apiSend("PATCH", `/api/templates/${id}`, input);
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/templates/${id}`);
  return { success: true };
}

export async function deleteTemplateAction(id: string) {
  const res = await apiSend("DELETE", `/api/templates/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/templates");
  redirect("/dashboard/templates");
}
