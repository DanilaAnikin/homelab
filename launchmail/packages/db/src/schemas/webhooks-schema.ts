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
  "email.suppressed",
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

// Transactional outbox: domain state and its webhook event are committed in
// the same PostgreSQL transaction. A relay moves rows to BullMQ afterwards;
// Redis downtime or a process crash can therefore delay, but not lose, events.
export const webhookOutbox = pgTable(
  "webhook_outbox",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    hookId: uuid("hook_id").references(() => webhooks.id, {
      onDelete: "set null",
    }),
    event: text("event").$type<WebhookEvent>().notNull(),
    data: jsonb("data").$type<unknown>().notNull(),
    occurredAt: timestamp("occurred_at", { withTimezone: true }).notNull(),
    idempotencyKey: text("idempotency_key").notNull().unique(),
    queuedAt: timestamp("queued_at"),
    completedAt: timestamp("completed_at"),
    failedAt: timestamp("failed_at"),
    lastError: text("last_error"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    index("webhook_outbox_pending_idx").on(
      table.queuedAt,
      table.failedAt,
      table.createdAt,
    ),
    index("webhook_outbox_hook_idx").on(table.hookId),
  ],
);

export type WebhookOutbox = typeof webhookOutbox.$inferSelect;
