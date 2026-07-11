import { db } from "@workspace/db";
import { emailTemplates } from "@workspace/db/schemas";
import type { EmailTemplate, EmailBlock } from "@workspace/db/schemas";
import { and, desc, eq } from "drizzle-orm";

export type { EmailTemplate, EmailBlock };

export interface CreateTemplateInput {
  name: string;
  mode: "html" | "blocks";
  subject?: string | null;
  html?: string | null;
  blocks?: EmailBlock[] | null;
  accent?: string | null;
  theme?: "light" | "dark" | null;
  createdByUserId?: string;
}

export interface UpdateTemplateInput {
  name?: string;
  mode?: "html" | "blocks";
  subject?: string | null;
  html?: string | null;
  blocks?: EmailBlock[] | null;
  accent?: string | null;
  theme?: "light" | "dark" | null;
}

export async function createTemplate(
  organizationId: string,
  input: CreateTemplateInput,
): Promise<EmailTemplate> {
  const [row] = await db
    .insert(emailTemplates)
    .values({
      organizationId,
      name: input.name,
      mode: input.mode,
      subject: input.subject ?? null,
      html: input.html ?? null,
      blocks: input.blocks ?? null,
      accent: input.accent ?? null,
      theme: input.theme ?? null,
      createdByUserId: input.createdByUserId ?? null,
    })
    .returning();
  return row!;
}

export async function listTemplates(
  organizationId: string,
): Promise<EmailTemplate[]> {
  return db
    .select()
    .from(emailTemplates)
    .where(eq(emailTemplates.organizationId, organizationId))
    .orderBy(desc(emailTemplates.updatedAt));
}

export async function getTemplate(
  id: string,
  organizationId: string,
): Promise<EmailTemplate | null> {
  const [row] = await db
    .select()
    .from(emailTemplates)
    .where(
      and(
        eq(emailTemplates.id, id),
        eq(emailTemplates.organizationId, organizationId),
      ),
    );
  return row ?? null;
}

export async function updateTemplate(
  id: string,
  organizationId: string,
  input: UpdateTemplateInput,
): Promise<EmailTemplate | null> {
  const updates: Partial<typeof emailTemplates.$inferInsert> = {
    updatedAt: new Date(),
  };
  if (input.name !== undefined) updates.name = input.name;
  if (input.mode !== undefined) updates.mode = input.mode;
  if (input.subject !== undefined) updates.subject = input.subject;
  if (input.html !== undefined) updates.html = input.html;
  if (input.blocks !== undefined) updates.blocks = input.blocks;
  if (input.accent !== undefined) updates.accent = input.accent;
  if (input.theme !== undefined) updates.theme = input.theme;

  const [row] = await db
    .update(emailTemplates)
    .set(updates)
    .where(
      and(
        eq(emailTemplates.id, id),
        eq(emailTemplates.organizationId, organizationId),
      ),
    )
    .returning();
  return row ?? null;
}

export async function deleteTemplate(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(emailTemplates)
    .where(
      and(
        eq(emailTemplates.id, id),
        eq(emailTemplates.organizationId, organizationId),
      ),
    )
    .returning();
  return rows.length > 0;
}
