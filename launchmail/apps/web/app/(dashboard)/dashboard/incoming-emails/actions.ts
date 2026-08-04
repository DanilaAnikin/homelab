"use server";

import { apiGet, apiSend } from "@/lib/api";
import { revalidatePath } from "next/cache";

export interface IncomingEmailFull {
  id: string;
  smtpConfigId: string;
  fromAddress: string;
  fromName: string | null;
  toAddresses: { email: string; name?: string }[];
  ccAddresses: { email: string; name?: string }[] | null;
  subject: string | null;
  text: string | null;
  html: string | null;
  sourceSizeBytes: number | null;
  sourceTruncated: boolean;
  contentTruncated: boolean;
  hasAttachments: boolean;
  attachments: { filename: string; contentType: string; size: number }[] | null;
  seen: boolean;
  starred: boolean;
  archived: boolean;
  repliedAt: string | null;
  receivedAt: string;
}

export interface SentEmailFull {
  id: string;
  smtpConfigId: string | null;
  from: string;
  to: { email: string; name?: string }[];
  subject: string;
  status: string;
  html: string | null;
  text: string | null;
  error: string | null;
  opens: number;
  clicks: number;
  providerMessageId: string | null;
  createdAt: string;
}

export interface OutgoingAttachment {
  filename: string;
  content: string; // base64
  contentType?: string;
}

const REVALIDATE = "/dashboard/incoming-emails";

/** Parse a comma/space/semicolon separated address string into recipients. */
function parseRecipients(raw?: string): { email: string }[] | undefined {
  if (!raw) return undefined;
  const list = raw
    .split(/[,;\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((email) => ({ email }));
  return list.length ? list : undefined;
}

export async function loadMessageAction(
  id: string,
): Promise<IncomingEmailFull | null> {
  return apiGet<IncomingEmailFull>(`/api/incoming-emails/${id}`);
}

export async function loadSentAction(
  id: string,
): Promise<SentEmailFull | null> {
  return apiGet<SentEmailFull>(`/api/sent-emails/${id}`);
}

export async function markReadAction(id: string, seen = true) {
  const res = await apiSend("POST", `/api/incoming-emails/${id}/read`, { seen });
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function starAction(id: string, starred: boolean) {
  const res = await apiSend("POST", `/api/incoming-emails/${id}/star`, {
    starred,
  });
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function archiveAction(id: string, archived: boolean) {
  const res = await apiSend("POST", `/api/incoming-emails/${id}/archive`, {
    archived,
  });
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function deleteMessageAction(id: string) {
  const res = await apiSend("DELETE", `/api/incoming-emails/${id}`);
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function blockSenderAction(id: string) {
  const res = await apiSend("POST", `/api/incoming-emails/${id}/block`, {});
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function replyAction(
  id: string,
  input: {
    text?: string;
    html?: string;
    cc?: string;
    bcc?: string;
    attachments?: OutgoingAttachment[];
  },
) {
  const res = await apiSend("POST", `/api/incoming-emails/${id}/reply`, {
    text: input.text,
    html: input.html,
    cc: parseRecipients(input.cc),
    bcc: parseRecipients(input.bcc),
    attachments: input.attachments,
  });
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function forwardAction(
  id: string,
  input: {
    to: string;
    cc?: string;
    bcc?: string;
    text?: string;
    html?: string;
    attachments?: OutgoingAttachment[];
  },
) {
  const to = parseRecipients(input.to);
  if (!to) return { error: "At least one recipient is required" };
  const res = await apiSend("POST", `/api/incoming-emails/${id}/forward`, {
    to,
    cc: parseRecipients(input.cc),
    bcc: parseRecipients(input.bcc),
    text: input.text,
    html: input.html,
    attachments: input.attachments,
  });
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { success: true };
}

export async function syncMailboxAction(smtpConfigId: string) {
  const res = await apiSend<{ fetched: number }>(
    "POST",
    "/api/incoming-emails/sync",
    { smtpConfigId },
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { fetched: res.data?.fetched ?? 0 };
}

export async function backfillAction(smtpConfigId: string) {
  const res = await apiSend<{ fetched: number; complete: boolean }>(
    "POST",
    "/api/incoming-emails/backfill",
    { smtpConfigId },
  );
  if (!res.ok) return { error: res.error };
  revalidatePath(REVALIDATE);
  return { fetched: res.data?.fetched ?? 0, complete: res.data?.complete ?? false };
}
