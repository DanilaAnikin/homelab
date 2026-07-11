"use server";

import { apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export async function createSmtpConfigAction(formData: FormData) {
  const imapHost = ((formData.get("imapHost") as string) || "").trim();
  const imapPort = parseInt(formData.get("imapPort") as string) || 993;
  const res = await apiSend<{ id: string }>("POST", "/api/smtp-configs", {
    name: formData.get("name") as string,
    host: formData.get("host") as string,
    port: parseInt(formData.get("port") as string) || 587,
    username: formData.get("username") as string,
    password: formData.get("password") as string,
    fromAddress: formData.get("fromAddress") as string,
    fromName: (formData.get("fromName") as string) || undefined,
    // Only send IMAP fields when a host is given. secure=false on 143 lets the
    // server upgrade via STARTTLS; implicit TLS otherwise (993 and friends).
    ...(imapHost
      ? {
          imapHost,
          imapPort,
          imapUsername: ((formData.get("imapUsername") as string) || "").trim() || undefined,
          imapPassword: (formData.get("imapPassword") as string) || undefined,
          imapSecure: imapPort !== 143,
        }
      : {}),
  });
  if (!res.ok) return { error: res.error };
  revalidatePath("/dashboard/smtp");
  return { id: res.data!.id };
}

export async function testImapRawAction(creds: {
  imapHost: string;
  imapPort?: number;
  imapUsername?: string;
  imapPassword: string;
  username?: string;
}) {
  const res = await apiSend<{ success: boolean; error?: string }>(
    "POST",
    "/api/smtp-configs/test-imap",
    creds,
  );
  if (!res.ok) {
    return { success: false as const, error: res.error || res.data?.error };
  }
  return { success: res.data?.success ?? false, error: res.data?.error };
}
