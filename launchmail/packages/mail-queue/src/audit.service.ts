import { db } from "@workspace/db";
import { auditLogs } from "@workspace/db/schemas";
import type { AuditLog } from "@workspace/db/schemas";
import { desc, eq } from "drizzle-orm";

export type { AuditLog };

export async function recordAudit(input: {
  organizationId: string;
  userId?: string | null;
  userName?: string | null;
  action: string;
  target?: string | null;
}): Promise<void> {
  await db
    .insert(auditLogs)
    .values({
      organizationId: input.organizationId,
      userId: input.userId ?? null,
      userName: input.userName ?? null,
      action: input.action,
      target: input.target ?? null,
    })
    .catch(() => undefined);
}

export async function listAudit(
  organizationId: string,
  limit = 50,
): Promise<AuditLog[]> {
  return db
    .select()
    .from(auditLogs)
    .where(eq(auditLogs.organizationId, organizationId))
    .orderBy(desc(auditLogs.createdAt))
    .limit(limit);
}
