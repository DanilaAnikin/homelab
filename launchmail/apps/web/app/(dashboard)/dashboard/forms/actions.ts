"use server";

import { apiGet, apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

interface UpdateFormInput {
  name?: string;
  templateKey?: string;
  recipients?: string[];
  smtpConfigId?: string | null;
  subject?: string | null;
  branding?: Record<string, unknown>;
  settings?: Record<string, unknown>;
  enabled?: boolean;
}

export async function createFormAction(input: {
  name: string;
  templateKey: string;
  templateId?: string | null;
  recipients: string;
  smtpConfigId: string;
  theme: "light" | "dark";
}) {
  if (!input.smtpConfigId) {
    return { error: "Choose an SMTP connection for this form" };
  }
  let recipients = input.recipients
    .split(/[,\s]+/)
    .map((r) => r.trim())
    .filter(Boolean);
  if (recipients.length === 0) {
    const session = await apiGet<{ user: { email: string } }>(
      "/api/auth/get-session",
    );
    if (session?.user?.email) recipients = [session.user.email];
  }
  if (recipients.length === 0) {
    return { error: "Add at least one recipient" };
  }

  const res = await apiSend<{ id: string }>("POST", "/api/forms", {
    name: input.name.trim() || "Untitled form",
    templateKey: input.templateKey,
    templateId: input.templateId ?? null,
    recipients,
    smtpConfigId: input.smtpConfigId,
    branding: { theme: input.theme },
  });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/forms");
  redirect(`/dashboard/forms/${res.data!.id}`);
}

export async function updateFormAction(id: string, input: UpdateFormInput) {
  const res = await apiSend("PATCH", `/api/forms/${id}`, input);
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/forms/${id}`);
  return { success: true };
}

export async function deleteFormAction(id: string) {
  const res = await apiSend("DELETE", `/api/forms/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/forms");
  redirect("/dashboard/forms");
}
