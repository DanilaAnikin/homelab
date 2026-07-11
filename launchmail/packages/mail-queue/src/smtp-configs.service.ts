import { db } from "@workspace/db";
import { smtpConfigs } from "@workspace/db/schemas";
import { eq, and, desc, isNotNull } from "drizzle-orm";
import { encrypt, decrypt } from "./crypto";

export interface SmtpConfig {
  id: string;
  organizationId: string;
  userId: string | null;
  name: string;
  // "smarthost" → relay via host/username/password below.
  // "direct" → deliver to recipient MX ourselves; credentials are null and
  // heloHostname/DKIM carry the identity instead. (See direct-transport.ts.)
  type: "smarthost" | "direct";
  host: string | null;
  port: number;
  username: string | null;
  password: string; // decrypted; "" for direct configs
  heloHostname: string | null;
  fromAddress: string;
  fromName: string | null;
  isDefault: boolean;
  // --- Incoming mail (IMAP). Receive-enabled when imapHost is set. imapPassword
  // is decrypted in-memory like `password`; never return it over HTTP.
  imapHost: string | null;
  imapPort: number | null;
  imapUsername: string | null;
  imapPassword: string | null;
  imapSecure: boolean | null;
  imapLastUid: number | null;
  imapUidValidity: number | null;
  imapFirstUid: number | null;
  imapBackfillComplete: boolean;
  imapLastSyncAt: Date | null;
  imapLastSyncError: string | null;
  createdAt: Date;
  updatedAt: Date;
}

export interface CreateSmtpConfigInput {
  name: string;
  // Omit type (or "smarthost") → host/username/password required.
  // "direct" → credentials omitted; heloHostname required instead.
  type?: "smarthost" | "direct";
  host?: string;
  port: number;
  username?: string;
  password?: string;
  heloHostname?: string | null;
  fromAddress: string;
  fromName?: string;
  createdByUserId?: string;
  imapHost?: string | null;
  imapPort?: number | null;
  imapUsername?: string | null;
  imapPassword?: string | null;
  imapSecure?: boolean | null;
}

export interface UpdateSmtpConfigInput {
  name?: string;
  type?: "smarthost" | "direct";
  host?: string;
  port?: number;
  username?: string;
  password?: string;
  heloHostname?: string | null;
  fromAddress?: string;
  fromName?: string;
  isDefault?: boolean;
  imapHost?: string | null;
  imapPort?: number | null;
  imapUsername?: string | null;
  imapPassword?: string | null;
  imapSecure?: boolean | null;
}

function toSmtpConfig(row: typeof smtpConfigs.$inferSelect): SmtpConfig {
  // Drop the ciphertexts so they can never ride along into API/CLI/MCP
  // responses; expose only the decrypted secrets (used internally by the
  // sender and the IMAP poller).
  const { passwordEncrypted, imapPasswordEncrypted, ...rest } = row;
  return {
    ...rest,
    // Direct configs have no upstream password → null ciphertext → "".
    type: (rest.type as "smarthost" | "direct") ?? "smarthost",
    password: passwordEncrypted ? decrypt(passwordEncrypted) : "",
    imapPassword: imapPasswordEncrypted ? decrypt(imapPasswordEncrypted) : null,
    fromName: rest.fromName ?? null,
  };
}

export async function listSmtpConfigs(
  organizationId: string,
): Promise<SmtpConfig[]> {
  const rows = await db
    .select()
    .from(smtpConfigs)
    .where(eq(smtpConfigs.organizationId, organizationId))
    .orderBy(desc(smtpConfigs.createdAt));
  return rows.map(toSmtpConfig);
}

export async function getSmtpConfig(
  id: string,
  organizationId: string,
): Promise<SmtpConfig | null> {
  const rows = await db
    .select()
    .from(smtpConfigs)
    .where(
      and(
        eq(smtpConfigs.id, id),
        eq(smtpConfigs.organizationId, organizationId),
      ),
    );
  const row = rows[0];
  return row ? toSmtpConfig(row) : null;
}

export async function getSmtpConfigById(
  id: string,
): Promise<SmtpConfig | null> {
  const rows = await db.select().from(smtpConfigs).where(eq(smtpConfigs.id, id));
  const row = rows[0];
  return row ? toSmtpConfig(row) : null;
}

export async function getDefaultSmtpConfig(
  organizationId: string,
): Promise<SmtpConfig | null> {
  const rows = await db
    .select()
    .from(smtpConfigs)
    .where(
      and(
        eq(smtpConfigs.organizationId, organizationId),
        eq(smtpConfigs.isDefault, true),
      ),
    )
    .limit(1);
  const row = rows[0];
  if (row) return toSmtpConfig(row);

  const fallback = await db
    .select()
    .from(smtpConfigs)
    .where(eq(smtpConfigs.organizationId, organizationId))
    .orderBy(desc(smtpConfigs.createdAt))
    .limit(1);
  return fallback[0] ? toSmtpConfig(fallback[0]) : null;
}

export async function createSmtpConfig(
  organizationId: string,
  input: CreateSmtpConfigInput,
): Promise<SmtpConfig> {
  // Decide-and-insert in one transaction so two concurrent creates can't both
  // see an empty org and both mark themselves default. The partial unique
  // index (one default per org) is the final backstop.
  const row = await db.transaction(async (tx) => {
    const existing = await tx
      .select({ id: smtpConfigs.id })
      .from(smtpConfigs)
      .where(eq(smtpConfigs.organizationId, organizationId))
      .limit(1);

    const isDefault = existing.length === 0;

    const [inserted] = await tx
      .insert(smtpConfigs)
      .values({
        organizationId,
        userId: input.createdByUserId ?? null,
        name: input.name,
        type: input.type ?? "smarthost",
        host: input.host ?? null,
        port: input.port,
        username: input.username ?? null,
        passwordEncrypted: input.password ? encrypt(input.password) : null,
        heloHostname: input.heloHostname ?? null,
        fromAddress: input.fromAddress,
        fromName: input.fromName ?? null,
        isDefault,
        imapHost: input.imapHost || null,
        imapPort: input.imapPort ?? null,
        imapUsername: input.imapUsername || null,
        imapPasswordEncrypted: input.imapPassword
          ? encrypt(input.imapPassword)
          : null,
        imapSecure: input.imapSecure ?? null,
      })
      .returning();

    return inserted!;
  });

  return toSmtpConfig(row);
}

export async function updateSmtpConfig(
  id: string,
  organizationId: string,
  input: UpdateSmtpConfigInput,
): Promise<SmtpConfig | null> {
  const existing = await getSmtpConfig(id, organizationId);
  if (!existing) return null;

  const updates: Record<string, unknown> = { updatedAt: new Date() };
  if (input.name !== undefined) updates.name = input.name;
  if (input.type !== undefined) updates.type = input.type;
  if (input.host !== undefined) updates.host = input.host || null;
  if (input.port !== undefined) updates.port = input.port;
  if (input.username !== undefined) updates.username = input.username || null;
  if (input.password !== undefined)
    updates.passwordEncrypted = input.password ? encrypt(input.password) : null;
  if (input.heloHostname !== undefined)
    updates.heloHostname = input.heloHostname || null;
  if (input.fromAddress !== undefined) updates.fromAddress = input.fromAddress;
  if (input.fromName !== undefined) updates.fromName = input.fromName;
  // IMAP fields. A field set to null/"" clears it (disabling receiving when the
  // host is cleared). When the host or login changes, drop the stored sync
  // cursor so the next poll re-syncs against the new mailbox rather than
  // skipping messages below a stale UID.
  let resetCursor = false;
  if (input.imapHost !== undefined) {
    const next = input.imapHost || null;
    updates.imapHost = next;
    if (next !== existing.imapHost) resetCursor = true;
  }
  if (input.imapPort !== undefined) updates.imapPort = input.imapPort ?? null;
  if (input.imapUsername !== undefined) {
    const next = input.imapUsername || null;
    updates.imapUsername = next;
    if (next !== existing.imapUsername) resetCursor = true;
  }
  // Omitted (undefined) keeps the current password; an explicit empty/null
  // clears it (consistent with create + the host/username fields).
  if (input.imapPassword !== undefined)
    updates.imapPasswordEncrypted = input.imapPassword
      ? encrypt(input.imapPassword)
      : null;
  if (input.imapSecure !== undefined) updates.imapSecure = input.imapSecure ?? null;
  if (resetCursor) {
    updates.imapLastUid = null;
    updates.imapUidValidity = null;
    updates.imapFirstUid = null;
    updates.imapBackfillComplete = false;
    updates.imapLastSyncAt = null;
    updates.imapLastSyncError = null;
  }

  // Promoting to default requires clearing the existing default first; do both
  // writes atomically so the partial unique index (one default per org) is
  // never transiently violated and concurrent promotions serialize.
  if (input.isDefault === true) {
    updates.isDefault = true;
    const row = await db.transaction(async (tx) => {
      await tx
        .update(smtpConfigs)
        .set({ isDefault: false })
        .where(
          and(
            eq(smtpConfigs.organizationId, organizationId),
            eq(smtpConfigs.isDefault, true),
          ),
        );
      const [updated] = await tx
        .update(smtpConfigs)
        .set(updates)
        .where(
          and(
            eq(smtpConfigs.id, id),
            eq(smtpConfigs.organizationId, organizationId),
          ),
        )
        .returning();
      return updated;
    });
    return row ? toSmtpConfig(row) : null;
  }

  const [row] = await db
    .update(smtpConfigs)
    .set(updates)
    .where(
      and(
        eq(smtpConfigs.id, id),
        eq(smtpConfigs.organizationId, organizationId),
      ),
    )
    .returning();

  return row ? toSmtpConfig(row) : null;
}

export async function deleteSmtpConfig(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(smtpConfigs)
    .where(
      and(
        eq(smtpConfigs.id, id),
        eq(smtpConfigs.organizationId, organizationId),
      ),
    )
    .returning({ id: smtpConfigs.id });
  return rows.length > 0;
}

/**
 * Every receive-enabled connection across all orgs — the IMAP poller iterates
 * these. Returns decrypted SmtpConfig objects (imapPassword populated), so the
 * result must stay server-side and never be serialized to a client.
 */
export async function listReceiveEnabledConfigs(): Promise<SmtpConfig[]> {
  const rows = await db
    .select()
    .from(smtpConfigs)
    .where(isNotNull(smtpConfigs.imapHost));
  return rows.map(toSmtpConfig);
}

/** Patch the per-mailbox IMAP sync state (cursors, backfill, health). */
export async function updateImapState(
  id: string,
  patch: {
    lastUid?: number;
    uidValidity?: number;
    firstUid?: number | null;
    backfillComplete?: boolean;
    lastSyncAt?: Date;
    lastSyncError?: string | null;
  },
): Promise<void> {
  const set: Record<string, unknown> = {};
  if (patch.lastUid !== undefined) set.imapLastUid = patch.lastUid;
  if (patch.uidValidity !== undefined) set.imapUidValidity = patch.uidValidity;
  if (patch.firstUid !== undefined) set.imapFirstUid = patch.firstUid;
  if (patch.backfillComplete !== undefined)
    set.imapBackfillComplete = patch.backfillComplete;
  if (patch.lastSyncAt !== undefined) set.imapLastSyncAt = patch.lastSyncAt;
  if (patch.lastSyncError !== undefined)
    set.imapLastSyncError = patch.lastSyncError;
  if (Object.keys(set).length === 0) return;
  await db.update(smtpConfigs).set(set).where(eq(smtpConfigs.id, id));
}
