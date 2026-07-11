import { db } from "@workspace/db";
import { audiences, contacts, broadcasts } from "@workspace/db/schemas";
import type { Audience, Contact, Broadcast } from "@workspace/db/schemas";
import { and, desc, eq, sql } from "drizzle-orm";
import { addSuppression } from "./suppressions.service";

export type { Audience, Contact, Broadcast };

export async function createAudience(
  organizationId: string,
  name: string,
): Promise<Audience> {
  const [row] = await db
    .insert(audiences)
    .values({ organizationId, name })
    .returning();
  return row!;
}

export async function listAudiences(
  organizationId: string,
): Promise<(Audience & { contactCount: number })[]> {
  const rows = await db
    .select({
      id: audiences.id,
      organizationId: audiences.organizationId,
      name: audiences.name,
      createdAt: audiences.createdAt,
      contactCount: sql<number>`count(${contacts.id})::int`,
    })
    .from(audiences)
    .leftJoin(contacts, eq(contacts.audienceId, audiences.id))
    .where(eq(audiences.organizationId, organizationId))
    .groupBy(audiences.id)
    .orderBy(desc(audiences.createdAt));
  return rows;
}

export async function getAudience(
  id: string,
  organizationId: string,
): Promise<Audience | null> {
  const [row] = await db
    .select()
    .from(audiences)
    .where(and(eq(audiences.id, id), eq(audiences.organizationId, organizationId)));
  return row ?? null;
}

export async function deleteAudience(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(audiences)
    .where(and(eq(audiences.id, id), eq(audiences.organizationId, organizationId)))
    .returning();
  return rows.length > 0;
}

export interface AddContactInput {
  email: string;
  firstName?: string;
  lastName?: string;
}

export async function addContacts(
  organizationId: string,
  audienceId: string,
  list: AddContactInput[],
): Promise<number> {
  if (list.length === 0) return 0;
  const rows = await db
    .insert(contacts)
    .values(
      list.map((c) => ({
        organizationId,
        audienceId,
        email: c.email.toLowerCase().trim(),
        firstName: c.firstName ?? null,
        lastName: c.lastName ?? null,
      })),
    )
    .onConflictDoNothing()
    .returning();
  return rows.length;
}

export async function listContacts(audienceId: string): Promise<Contact[]> {
  return db
    .select()
    .from(contacts)
    .where(eq(contacts.audienceId, audienceId))
    .orderBy(desc(contacts.createdAt));
}

export async function getSubscribedContacts(
  audienceId: string,
): Promise<Contact[]> {
  return db
    .select()
    .from(contacts)
    .where(and(eq(contacts.audienceId, audienceId), eq(contacts.subscribed, true)));
}

export async function removeContact(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(contacts)
    .where(and(eq(contacts.id, id), eq(contacts.organizationId, organizationId)))
    .returning();
  return rows.length > 0;
}

export async function unsubscribeContact(id: string): Promise<Contact | null> {
  // Look up the contact (and its org) first so we can suppress org-wide.
  const [target] = await db
    .select()
    .from(contacts)
    .where(eq(contacts.id, id))
    .limit(1);
  if (!target) return null;

  // Flip subscribed=false for *every* contact row in this org with the same
  // email (the address may appear across multiple audiences), and stamp the
  // unsubscribe time on the clicked row.
  await db
    .update(contacts)
    .set({ subscribed: false, unsubscribedAt: new Date() })
    .where(
      and(
        eq(contacts.organizationId, target.organizationId),
        eq(contacts.email, target.email),
      ),
    );

  // Add to the org suppression list so filterSuppressed blocks this address
  // everywhere (transactional sends and broadcasts alike), not just in lists.
  await addSuppression(target.organizationId, target.email, "unsubscribe").catch(
    () => undefined,
  );

  const [row] = await db
    .select()
    .from(contacts)
    .where(eq(contacts.id, id))
    .limit(1);
  return row ?? null;
}

export interface CreateBroadcastInput {
  audienceId: string;
  smtpConfigId?: string | null;
  templateId?: string | null;
  name: string;
  subject: string;
  totalCount: number;
}

export async function createBroadcast(
  organizationId: string,
  input: CreateBroadcastInput,
): Promise<Broadcast> {
  const [row] = await db
    .insert(broadcasts)
    .values({
      organizationId,
      audienceId: input.audienceId,
      smtpConfigId: input.smtpConfigId ?? null,
      templateId: input.templateId ?? null,
      name: input.name,
      subject: input.subject,
      status: "sending",
      totalCount: input.totalCount,
    })
    .returning();
  return row!;
}

export async function completeBroadcast(
  id: string,
  sentCount: number,
): Promise<Broadcast | undefined> {
  const [row] = await db
    .update(broadcasts)
    .set({ status: "sent", sentCount, sentAt: new Date() })
    .where(eq(broadcasts.id, id))
    .returning();
  return row;
}

export async function listBroadcasts(
  organizationId: string,
): Promise<Broadcast[]> {
  return db
    .select()
    .from(broadcasts)
    .where(eq(broadcasts.organizationId, organizationId))
    .orderBy(desc(broadcasts.createdAt));
}
