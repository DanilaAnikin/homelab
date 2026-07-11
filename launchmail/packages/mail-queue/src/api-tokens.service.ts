import { db } from "@workspace/db";
import { apiTokens } from "@workspace/db/schemas";
import { eq, and, desc } from "drizzle-orm";
import crypto from "node:crypto";

export type ApiTokenRole = "admin" | "writer" | "reader";

export interface ApiToken {
  id: string;
  organizationId: string;
  userId: string | null;
  name: string;
  role: ApiTokenRole;
  tokenHash: string;
  tokenPrefix: string;
  smtpConfigId: string | null;
  lastUsedAt: Date | null;
  expiresAt: Date | null;
  createdAt: Date;
}

export interface ApiTokenWithPlaintext extends ApiToken {
  plaintext: string; // only returned once on creation
}

function generateToken(): { plaintext: string; hash: string; prefix: string } {
  const plaintext = `lm_${crypto.randomBytes(24).toString("hex")}`;
  const hash = crypto.createHash("sha256").update(plaintext).digest("hex");
  const prefix = plaintext.slice(0, 11);
  return { plaintext, hash, prefix };
}

export function hashToken(token: string): string {
  return crypto.createHash("sha256").update(token).digest("hex");
}

export interface CreateApiTokenInput {
  name: string;
  role?: ApiTokenRole;
  smtpConfigId?: string | null;
  expiresAt?: Date;
  createdByUserId?: string;
}

export async function createApiToken(
  organizationId: string,
  input: CreateApiTokenInput,
): Promise<ApiTokenWithPlaintext> {
  const { plaintext, hash, prefix } = generateToken();

  const [row] = await db
    .insert(apiTokens)
    .values({
      organizationId,
      userId: input.createdByUserId ?? null,
      name: input.name,
      role: input.role ?? "writer",
      tokenHash: hash,
      tokenPrefix: prefix,
      smtpConfigId: input.smtpConfigId ?? null,
      expiresAt: input.expiresAt ?? null,
    })
    .returning();

  return { ...row!, plaintext };
}

export async function listApiTokens(
  organizationId: string,
): Promise<ApiToken[]> {
  return db
    .select()
    .from(apiTokens)
    .where(eq(apiTokens.organizationId, organizationId))
    .orderBy(desc(apiTokens.createdAt));
}

export async function deleteApiToken(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(apiTokens)
    .where(
      and(eq(apiTokens.id, id), eq(apiTokens.organizationId, organizationId)),
    )
    .returning({ id: apiTokens.id });
  return rows.length > 0;
}

export async function findApiTokenByHash(
  hash: string,
): Promise<ApiToken | null> {
  const rows = await db
    .select()
    .from(apiTokens)
    .where(eq(apiTokens.tokenHash, hash));
  return rows[0] ?? null;
}

export async function touchApiToken(id: string): Promise<void> {
  await db
    .update(apiTokens)
    .set({ lastUsedAt: new Date() })
    .where(eq(apiTokens.id, id));
}
