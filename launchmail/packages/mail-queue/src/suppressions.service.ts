import { db } from "@workspace/db";
import { suppressions } from "@workspace/db/schemas";
import type { Suppression } from "@workspace/db/schemas";
import { and, desc, eq, inArray } from "drizzle-orm";

export type { Suppression };

export async function addSuppression(
  organizationId: string,
  email: string,
  reason = "manual",
): Promise<void> {
  await db
    .insert(suppressions)
    .values({ organizationId, email: email.toLowerCase().trim(), reason })
    .onConflictDoNothing();
}

export async function listSuppressions(
  organizationId: string,
): Promise<Suppression[]> {
  return db
    .select()
    .from(suppressions)
    .where(eq(suppressions.organizationId, organizationId))
    .orderBy(desc(suppressions.createdAt));
}

export async function removeSuppression(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(suppressions)
    .where(
      and(
        eq(suppressions.id, id),
        eq(suppressions.organizationId, organizationId),
      ),
    )
    .returning();
  return rows.length > 0;
}

export async function isSuppressed(
  organizationId: string,
  email: string,
): Promise<boolean> {
  const [row] = await db
    .select({ id: suppressions.id })
    .from(suppressions)
    .where(
      and(
        eq(suppressions.organizationId, organizationId),
        eq(suppressions.email, email.toLowerCase().trim()),
      ),
    );
  return !!row;
}

export async function filterSuppressed(
  organizationId: string,
  emails: string[],
): Promise<{ allowed: string[]; suppressed: string[] }> {
  if (emails.length === 0) return { allowed: [], suppressed: [] };
  const lowered = emails.map((e) => e.toLowerCase().trim());
  const rows = await db
    .select({ email: suppressions.email })
    .from(suppressions)
    .where(
      and(
        eq(suppressions.organizationId, organizationId),
        inArray(suppressions.email, lowered),
      ),
    );
  const sup = new Set(rows.map((r) => r.email));
  return {
    allowed: emails.filter((e) => !sup.has(e.toLowerCase().trim())),
    suppressed: emails.filter((e) => sup.has(e.toLowerCase().trim())),
  };
}
