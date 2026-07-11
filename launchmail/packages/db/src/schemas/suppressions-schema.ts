import {
  pgTable,
  text,
  timestamp,
  uuid,
  unique,
  index,
} from "drizzle-orm/pg-core";
import { organization } from "./auth-schema";

export const suppressions = pgTable(
  "suppressions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    email: text("email").notNull(),
    reason: text("reason").notNull().default("manual"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    unique("suppressions_org_email_uniq").on(table.organizationId, table.email),
    index("suppressions_organizationId_idx").on(table.organizationId),
  ],
);

export type Suppression = typeof suppressions.$inferSelect;
