import {
  pgTable,
  text,
  jsonb,
  timestamp,
  uuid,
  index,
} from "drizzle-orm/pg-core";
import { relations } from "drizzle-orm";
import { organization, user } from "./auth-schema";

export type EmailBlock =
  | { id: string; type: "heading"; text: string }
  | { id: string; type: "text"; text: string }
  | { id: string; type: "button"; text: string; url: string }
  | { id: string; type: "image"; url: string; alt?: string }
  | { id: string; type: "divider" }
  | { id: string; type: "spacer"; size?: number };

export const emailTemplates = pgTable(
  "email_templates",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    name: text("name").notNull(),
    mode: text("mode").notNull().default("blocks"),
    subject: text("subject"),
    html: text("html"),
    blocks: jsonb("blocks").$type<EmailBlock[]>(),
    accent: text("accent"),
    theme: text("theme").$type<"light" | "dark">(),
    createdByUserId: text("created_by_user_id").references(() => user.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    index("email_templates_organizationId_idx").on(table.organizationId),
  ],
);

export const emailTemplatesRelations = relations(emailTemplates, ({ one }) => ({
  organization: one(organization, {
    fields: [emailTemplates.organizationId],
    references: [organization.id],
  }),
}));

export type EmailTemplate = typeof emailTemplates.$inferSelect;
