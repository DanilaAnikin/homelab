import {
  pgTable,
  text,
  timestamp,
  uuid,
  boolean,
  unique,
  index,
} from "drizzle-orm/pg-core";
import { organization } from "./auth-schema";

export const domains = pgTable(
  "domains",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    domain: text("domain").notNull(),
    dkimSelector: text("dkim_selector").notNull().default("launchmail"),
    dkimPublicKey: text("dkim_public_key"),
    dkimPrivateKey: text("dkim_private_key"),
    spfVerified: boolean("spf_verified").notNull().default(false),
    dkimVerified: boolean("dkim_verified").notNull().default(false),
    dmarcVerified: boolean("dmarc_verified").notNull().default(false),
    verified: boolean("verified").notNull().default(false),
    lastCheckedAt: timestamp("last_checked_at"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    unique("domains_org_domain_uniq").on(table.organizationId, table.domain),
    index("domains_organizationId_idx").on(table.organizationId),
  ],
);

export type Domain = typeof domains.$inferSelect;
