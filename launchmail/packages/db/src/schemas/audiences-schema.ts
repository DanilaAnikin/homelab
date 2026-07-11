import {
  pgTable,
  text,
  timestamp,
  uuid,
  boolean,
  integer,
  unique,
  index,
} from "drizzle-orm/pg-core";
import { organization } from "./auth-schema";
import { smtpConfigs } from "./mail-schema";
import { emailTemplates } from "./templates-schema";

export const audiences = pgTable(
  "audiences",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    name: text("name").notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [index("audiences_organizationId_idx").on(table.organizationId)],
);

export const contacts = pgTable(
  "contacts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    audienceId: uuid("audience_id")
      .notNull()
      .references(() => audiences.id, { onDelete: "cascade" }),
    email: text("email").notNull(),
    firstName: text("first_name"),
    lastName: text("last_name"),
    subscribed: boolean("subscribed").notNull().default(true),
    unsubscribedAt: timestamp("unsubscribed_at"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    unique("contacts_audience_email_uniq").on(table.audienceId, table.email),
    index("contacts_audienceId_idx").on(table.audienceId),
  ],
);

export const broadcasts = pgTable(
  "broadcasts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    audienceId: uuid("audience_id")
      .notNull()
      .references(() => audiences.id, { onDelete: "cascade" }),
    smtpConfigId: uuid("smtp_config_id").references(() => smtpConfigs.id, {
      onDelete: "set null",
    }),
    templateId: uuid("template_id").references(() => emailTemplates.id, {
      onDelete: "set null",
    }),
    name: text("name").notNull(),
    subject: text("subject").notNull(),
    status: text("status").notNull().default("draft"),
    totalCount: integer("total_count").notNull().default(0),
    sentCount: integer("sent_count").notNull().default(0),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    sentAt: timestamp("sent_at"),
  },
  (table) => [index("broadcasts_organizationId_idx").on(table.organizationId)],
);

export type Audience = typeof audiences.$inferSelect;
export type Contact = typeof contacts.$inferSelect;
export type Broadcast = typeof broadcasts.$inferSelect;
