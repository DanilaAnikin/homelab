import { generateKeyPairSync } from "node:crypto";
import { promises as dns } from "node:dns";
import { db } from "@workspace/db";
import { domains } from "@workspace/db/schemas";
import type { Domain } from "@workspace/db/schemas";
import { and, desc, eq } from "drizzle-orm";
import { encrypt, decrypt } from "./crypto";
import { checkDeliverability, type DeliverabilityReport } from "./deliverability";
import { listSmtpConfigs } from "./smtp-configs.service";

export type { Domain };
export type { DeliverabilityReport };

export interface DnsRecord {
  type: "TXT";
  name: string;
  value: string;
  purpose: "SPF" | "DKIM" | "DMARC";
  verified: boolean;
}

function generateDkim(): { publicKey: string; privateKey: string } {
  const { publicKey, privateKey } = generateKeyPairSync("rsa", {
    modulusLength: 2048,
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
  });
  return { publicKey, privateKey };
}

function dkimPublicTxt(pem: string): string {
  const b64 = pem
    .replace(/-----BEGIN PUBLIC KEY-----/g, "")
    .replace(/-----END PUBLIC KEY-----/g, "")
    .replace(/\s+/g, "");
  return `v=DKIM1; k=rsa; p=${b64}`;
}

export function buildRecords(domain: Domain): DnsRecord[] {
  const root = domain.domain;
  return [
    {
      type: "TXT",
      name: root,
      value: "v=spf1 a mx ~all",
      purpose: "SPF",
      verified: domain.spfVerified,
    },
    {
      type: "TXT",
      name: `${domain.dkimSelector}._domainkey.${root}`,
      value: domain.dkimPublicKey
        ? dkimPublicTxt(domain.dkimPublicKey)
        : "",
      purpose: "DKIM",
      verified: domain.dkimVerified,
    },
    {
      type: "TXT",
      name: `_dmarc.${root}`,
      value: "v=DMARC1; p=none; rua=mailto:dmarc@" + root,
      purpose: "DMARC",
      verified: domain.dmarcVerified,
    },
  ];
}

export async function createDomain(
  organizationId: string,
  domainName: string,
): Promise<Domain> {
  const { publicKey, privateKey } = generateDkim();
  const [row] = await db
    .insert(domains)
    .values({
      organizationId,
      domain: domainName.toLowerCase().trim(),
      dkimSelector: "launchmail",
      dkimPublicKey: publicKey,
      dkimPrivateKey: encrypt(privateKey),
    })
    .returning();
  return row!;
}

export async function listDomains(organizationId: string): Promise<Domain[]> {
  return db
    .select()
    .from(domains)
    .where(eq(domains.organizationId, organizationId))
    .orderBy(desc(domains.createdAt));
}

export async function getDomain(
  id: string,
  organizationId: string,
): Promise<Domain | null> {
  const [row] = await db
    .select()
    .from(domains)
    .where(and(eq(domains.id, id), eq(domains.organizationId, organizationId)));
  return row ?? null;
}

export async function deleteDomain(
  id: string,
  organizationId: string,
): Promise<boolean> {
  const rows = await db
    .delete(domains)
    .where(and(eq(domains.id, id), eq(domains.organizationId, organizationId)))
    .returning();
  return rows.length > 0;
}

export function getDkimPrivateKey(domain: Domain): string | null {
  return domain.dkimPrivateKey ? decrypt(domain.dkimPrivateKey) : null;
}

export interface DkimSigningKey {
  domainName: string;
  keySelector: string;
  privateKey: string;
}

// Returns DKIM signing params if the From address's domain is a DKIM-verified
// sending domain for the org — so outgoing mail from that domain is signed.
export async function findSigningDomain(
  organizationId: string,
  fromAddress: string,
): Promise<DkimSigningKey | null> {
  const match = fromAddress.match(/@([^@>\s]+)/);
  if (!match) return null;
  const domainPart = match[1]!.toLowerCase().trim();
  const [row] = await db
    .select()
    .from(domains)
    .where(
      and(
        eq(domains.organizationId, organizationId),
        eq(domains.domain, domainPart),
        eq(domains.dkimVerified, true),
      ),
    );
  if (!row || !row.dkimPrivateKey) return null;
  return {
    domainName: row.domain,
    keySelector: row.dkimSelector,
    privateKey: decrypt(row.dkimPrivateKey),
  };
}

async function resolveTxtFlat(name: string): Promise<string[]> {
  try {
    const records = await dns.resolveTxt(name);
    return records.map((parts) => parts.join(""));
  } catch {
    return [];
  }
}

export async function verifyDomain(
  id: string,
  organizationId: string,
): Promise<Domain | null> {
  const domain = await getDomain(id, organizationId);
  if (!domain) return null;

  const [spfTxt, dkimTxt, dmarcTxt] = await Promise.all([
    resolveTxtFlat(domain.domain),
    resolveTxtFlat(`${domain.dkimSelector}._domainkey.${domain.domain}`),
    resolveTxtFlat(`_dmarc.${domain.domain}`),
  ]);

  const expectedDkim = domain.dkimPublicKey
    ? dkimPublicTxt(domain.dkimPublicKey).replace(/\s+/g, "")
    : "__none__";

  const spfVerified = spfTxt.some((t) => t.toLowerCase().includes("v=spf1"));
  const dkimVerified = dkimTxt.some(
    (t) => t.replace(/\s+/g, "") === expectedDkim,
  );
  const dmarcVerified = dmarcTxt.some((t) =>
    t.toLowerCase().includes("v=dmarc1"),
  );

  const [row] = await db
    .update(domains)
    .set({
      spfVerified,
      dkimVerified,
      dmarcVerified,
      verified: spfVerified && dkimVerified,
      lastCheckedAt: new Date(),
    })
    .where(and(eq(domains.id, id), eq(domains.organizationId, organizationId)))
    .returning();
  return row ?? null;
}

/**
 * Full deliverability report for a sending domain (Phase 4). Discovers the
 * org's direct-delivery egress host (from its "direct" smtp_config), resolves
 * its IP, then checks SPF/DKIM/DMARC/PTR/port-25 and generates the DNS records
 * to publish. Falls back to record-less checks when no direct config exists.
 */
export async function domainDeliverability(
  domain: Domain,
  organizationId: string,
): Promise<DeliverabilityReport> {
  const configs = await listSmtpConfigs(organizationId);
  const direct = configs.find((c) => c.type === "direct" && c.heloHostname);
  const heloHostname = direct?.heloHostname ?? undefined;

  let egressIp: string | undefined;
  if (heloHostname) {
    const a = await dns.resolve4(heloHostname).catch(() => [] as string[]);
    egressIp = a[0];
  }

  return checkDeliverability({
    domain: domain.domain,
    dkimSelector: domain.dkimSelector,
    dkimPublicKeyTxt: domain.dkimPublicKey
      ? dkimPublicTxt(domain.dkimPublicKey)
      : "",
    egressIp,
    heloHostname,
  });
}
