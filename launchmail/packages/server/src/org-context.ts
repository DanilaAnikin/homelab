import { randomUUID } from "node:crypto";
import { db } from "@workspace/db";
import { member, organization, user } from "@workspace/db/schemas";
import { and, eq, sql } from "drizzle-orm";
import { hasPermission, isAppRole, type AppRole } from "@workspace/auth/permissions";
import type { Context } from "hono";
import type { AppVariables } from ".";

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "workspace"
  );
}

export async function ensurePersonalOrg(
  userId: string,
): Promise<{ organizationId: string | null; role: AppRole | null }> {
  // Fast path: most calls find an existing membership and never need the lock.
  const existing = await db
    .select({ organizationId: member.organizationId, role: member.role })
    .from(member)
    .where(eq(member.userId, userId))
    .limit(1);
  if (existing[0]) {
    return {
      organizationId: existing[0].organizationId,
      role: isAppRole(existing[0].role) ? existing[0].role : "reader",
    };
  }

  // No membership yet: create one atomically. Concurrent /api/me calls for the
  // same user must not each create a duplicate workspace, so serialize on a
  // per-user advisory lock held for the transaction and re-check inside it.
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${userId}))`);

    const locked = await tx
      .select({ organizationId: member.organizationId, role: member.role })
      .from(member)
      .where(eq(member.userId, userId))
      .limit(1);
    if (locked[0]) {
      return {
        organizationId: locked[0].organizationId,
        role: isAppRole(locked[0].role) ? locked[0].role : "reader",
      };
    }

    const [u] = await tx
      .select({ name: user.name })
      .from(user)
      .where(eq(user.id, userId));
    if (!u) return { organizationId: null, role: null };

    const orgId = randomUUID();
    await tx.insert(organization).values({
      id: orgId,
      name: `${u.name || "My"}'s Workspace`,
      slug: `${slugify(u.name)}-${orgId.slice(0, 6)}`,
    });
    await tx.insert(member).values({
      id: randomUUID(),
      organizationId: orgId,
      userId,
      role: "admin",
    });
    return { organizationId: orgId, role: "admin" };
  });
}

export async function resolveSessionOrg(
  userId: string,
  activeOrganizationId: string | null | undefined,
): Promise<{ organizationId: string | null; role: AppRole | null }> {
  const activeId = activeOrganizationId ?? null;

  if (activeId) {
    const m = await db
      .select({ role: member.role })
      .from(member)
      .where(and(eq(member.userId, userId), eq(member.organizationId, activeId)));
    if (m[0]) {
      return { organizationId: activeId, role: isAppRole(m[0].role) ? m[0].role : "reader" };
    }
  }

  const first = await db
    .select({ organizationId: member.organizationId, role: member.role })
    .from(member)
    .where(eq(member.userId, userId))
    .limit(1);

  if (first[0]) {
    return {
      organizationId: first[0].organizationId,
      role: isAppRole(first[0].role) ? first[0].role : "reader",
    };
  }

  return { organizationId: null, role: null };
}

export function requirePerm(
  c: Context<AppVariables>,
  resource: Parameters<typeof hasPermission>[1],
  action: string,
): Response | null {
  const organizationId = c.get("organizationId");
  const role = c.get("role");
  if (!organizationId || !role) {
    return c.json({ error: "Authentication required" }, 401);
  }
  if (!hasPermission(role, resource, action)) {
    return c.json({ error: "Insufficient permissions" }, 403);
  }
  return null;
}
