import crypto from "node:crypto";
import { db } from "@workspace/db";
import { forms, formSubmissions } from "@workspace/db/schemas";
import type {
  FormBranding,
  FormSettings,
  SubmissionMeta,
} from "@workspace/db/schemas";
import { and, desc, eq } from "drizzle-orm";

export type Form = typeof forms.$inferSelect;
export type FormSubmission = typeof formSubmissions.$inferSelect;

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "form"
  );
}

export interface CreateFormInput {
  name: string;
  templateKey: string;
  templateId?: string | null;
  recipients: string[];
  subject?: string;
  replyToField?: string;
  smtpConfigId?: string | null;
  branding?: FormBranding;
  settings?: FormSettings;
  createdByUserId?: string;
}

export interface UpdateFormInput {
  name?: string;
  templateKey?: string;
  templateId?: string | null;
  recipients?: string[];
  smtpConfigId?: string | null;
  subject?: string | null;
  replyToField?: string | null;
  branding?: FormBranding;
  customHtml?: string | null;
  settings?: FormSettings;
  enabled?: boolean;
}

export async function createForm(
  organizationId: string,
  input: CreateFormInput,
): Promise<Form> {
  const endpointToken = `f_${crypto.randomBytes(12).toString("hex")}`;
  const [row] = await db
    .insert(forms)
    .values({
      organizationId,
      smtpConfigId: input.smtpConfigId ?? null,
      templateId: input.templateId ?? null,
      name: input.name,
      slug: slugify(input.name),
      endpointToken,
      templateKey: input.templateKey,
      recipients: input.recipients,
      subject: input.subject ?? null,
      replyToField: input.replyToField ?? "email",
      branding: input.branding ?? null,
      settings: input.settings ?? { honeypot: true, storeSubmissions: true },
      createdByUserId: input.createdByUserId ?? null,
    })
    .returning();
  return row!;
}

export async function listForms(organizationId: string): Promise<Form[]> {
  return db
    .select()
    .from(forms)
    .where(eq(forms.organizationId, organizationId))
    .orderBy(desc(forms.createdAt));
}

export async function getForm(
  id: string,
  organizationId: string,
): Promise<Form | null> {
  const [row] = await db
    .select()
    .from(forms)
    .where(and(eq(forms.id, id), eq(forms.organizationId, organizationId)));
  return row ?? null;
}

export async function getFormByToken(token: string): Promise<Form | null> {
  const [row] = await db
    .select()
    .from(forms)
    .where(eq(forms.endpointToken, token));
  return row ?? null;
}

export async function updateForm(
  id: string,
  organizationId: string,
  input: UpdateFormInput,
): Promise<Form | null> {
  const updates: Partial<typeof forms.$inferInsert> = { updatedAt: new Date() };
  if (input.name !== undefined) {
    updates.name = input.name;
    updates.slug = slugify(input.name);
  }
  if (input.templateKey !== undefined) updates.templateKey = input.templateKey;
  if (input.templateId !== undefined) updates.templateId = input.templateId;
  if (input.smtpConfigId !== undefined)
    updates.smtpConfigId = input.smtpConfigId;
  if (input.recipients !== undefined) updates.recipients = input.recipients;
  if (input.subject !== undefined) updates.subject = input.subject;
  if (input.replyToField !== undefined)
    updates.replyToField = input.replyToField;
  if (input.branding !== undefined) updates.branding = input.branding;
  if (input.customHtml !== undefined) updates.customHtml = input.customHtml;
  if (input.settings !== undefined) updates.settings = input.settings;
  if (input.enabled !== undefined) updates.enabled = input.enabled;

  const [row] = await db
    .update(forms)
    .set(updates)
    .where(and(eq(forms.id, id), eq(forms.organizationId, organizationId)))
    .returning();
  return row ?? null;
}

export async function deleteForm(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(forms)
    .where(and(eq(forms.id, id), eq(forms.organizationId, organizationId)))
    .returning({ id: forms.id });
  return rows.length > 0;
}

export async function recordSubmission(
  formId: string,
  data: Record<string, string>,
  meta: SubmissionMeta,
  emailStatus = "queued",
): Promise<FormSubmission> {
  const [row] = await db
    .insert(formSubmissions)
    .values({ formId, data, meta, emailStatus })
    .returning();
  return row!;
}

export async function listSubmissions(
  formId: string,
  limit = 100,
): Promise<FormSubmission[]> {
  return db
    .select()
    .from(formSubmissions)
    .where(eq(formSubmissions.formId, formId))
    .orderBy(desc(formSubmissions.createdAt))
    .limit(limit);
}
