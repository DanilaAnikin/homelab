"use server";

import { apiGet, apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export async function updateSmtpConfigAction(id: string, formData: FormData) {
  const password = formData.get("password") as string;
  const imapHost = ((formData.get("imapHost") as string) ?? "").trim();
  const imapPort = parseInt(formData.get("imapPort") as string) || 993;
  const imapPassword = formData.get("imapPassword") as string;
  const body: Record<string, unknown> = {
    name: formData.get("name") as string,
    host: formData.get("host") as string,
    port: parseInt(formData.get("port") as string) || 587,
    username: formData.get("username") as string,
    fromAddress: formData.get("fromAddress") as string,
    fromName: (formData.get("fromName") as string) || undefined,
    isDefault: formData.get("isDefault") === "on",
    // Always send imapHost so clearing it disables receiving. The rest only
    // matter when a host is present.
    imapHost,
    imapPort: imapHost ? imapPort : null,
    imapUsername: imapHost
      ? ((formData.get("imapUsername") as string) || "").trim()
      : "",
    imapSecure: imapHost ? imapPort !== 143 : null,
  };
  if (password) body.password = password;
  if (imapPassword) body.imapPassword = imapPassword;

  const res = await apiSend("PATCH", `/api/smtp-configs/${id}`, body);
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/smtp/${id}`);
  return { success: true };
}

export async function deleteSmtpConfigAction(id: string) {
  const res = await apiSend("DELETE", `/api/smtp-configs/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/smtp");
  return { success: true };
}

export async function testImapSavedAction(id: string) {
  const res = await apiSend<{ success: boolean; error?: string }>(
    "POST",
    `/api/smtp-configs/${id}/test-imap`,
  );
  if (!res.ok) {
    return { success: false as const, error: res.error || res.data?.error };
  }
  return { success: res.data?.success ?? false, error: res.data?.error };
}

export async function testSmtpConnectionAction(id: string) {
  const res = await apiSend<{ success: boolean; error?: string }>(
    "POST",
    `/api/smtp-configs/${id}/test`,
  );
  if (!res.ok) return { success: false as const, error: res.error };
  return {
    success: res.data?.success ?? false,
    error: res.data?.error,
  };
}

export async function testSendEmailAction(id: string, to: string) {
  const res = await apiSend<{ success: boolean; messageId?: string }>(
    "POST",
    `/api/smtp-configs/${id}/test-send`,
    { to },
  );
  if (!res.ok) return { success: false as const, error: res.error };
  return { success: true as const, messageId: res.data?.messageId };
}

export async function createApiTokenAction(smtpConfigId: string, name: string) {
  const res = await apiSend<{ token: string; id: string }>(
    "POST",
    `/api/smtp-configs/${smtpConfigId}/tokens`,
    { name, role: "writer" },
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(`/dashboard/smtp/${smtpConfigId}`);
  return { token: res.data!.token, id: res.data!.id };
}

export async function deleteApiTokenAction(tokenId: string) {
  const res = await apiSend("DELETE", `/api/api-keys/${tokenId}`);
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/smtp");
  return { success: true };
}

export async function listApiTokensAction(smtpConfigId: string) {
  const tokens =
    (await apiGet<
      {
        id: string;
        name: string;
        tokenPrefix: string;
        createdAt: string;
        lastUsedAt: string | null;
      }[]
    >(`/api/smtp-configs/${smtpConfigId}/tokens`)) ?? [];
  return tokens.map((t) => ({
    id: t.id,
    name: t.name,
    tokenPrefix: t.tokenPrefix,
    createdAt: t.createdAt,
    lastUsedAt: t.lastUsedAt,
  }));
}
