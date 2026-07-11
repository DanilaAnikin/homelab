import {
  pgTable,
  text,
  jsonb,
  timestamp,
  uuid,
  boolean,
  index,
} from "drizzle-orm/pg-core";
import { organization } from "./auth-schema";

export const WEBHOOK_EVENTS = [
  "email.sent",
  "email.failed",
  "email.bounced",
  "form.submission",
  "incoming.received",
] as const;

export type WebhookEvent = (typeof WEBHOOK_EVENTS)[number];

export const webhooks = pgTable(
  "webhooks",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    url: text("url").notNull(),
    events: jsonb("events").$type<WebhookEvent[]>().notNull(),
    secret: text("secret").notNull(),
    enabled: boolean("enabled").notNull().default(true),
    lastStatus: text("last_status"),
    lastDeliveredAt: timestamp("last_delivered_at"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [index("webhooks_organizationId_idx").on(table.organizationId)],
);

export type Webhook = typeof webhooks.$inferSelect;
