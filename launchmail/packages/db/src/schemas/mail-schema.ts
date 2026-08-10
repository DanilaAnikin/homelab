import {
  pgTable,
  text,
  integer,
  bigint,
  jsonb,
  timestamp,
  uuid,
  boolean,
  varchar,
  index,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";
import { user, organization } from "./auth-schema";

export const smtpConfigs = pgTable(
  "smtp_configs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    userId: text("user_id").references(() => user.id, {
      onDelete: "set null",
    }),
    name: text("name").notNull(),
    // Delivery mode. "smarthost" relays through an upstream SMTP server
    // (host/username/password required — the classic path). "direct" resolves
    // each recipient's MX and delivers on port 25 itself, with no upstream
    // credentials — our own-ESP path. See packages/mail-queue/src/direct-transport.ts.
    type: text("type").notNull().default("smarthost"),
    // Smarthost credentials. NULL for "direct" configs (no upstream to log in to).
    host: text("host"),
    port: integer("port").notNull().default(587),
    username: text("username"),
    passwordEncrypted: text("password_encrypted"),
    // EHLO/HELO identity used for "direct" sends. MUST match the PTR (reverse
    // DNS) of the egress IP, or receivers fail forward-confirmed rDNS and reject.
    heloHostname: text("helo_hostname"),
    fromAddress: text("from_address").notNull(),
    fromName: text("from_name"),
    isDefault: boolean("is_default").notNull().default(false),
    // --- Incoming mail (IMAP) — all nullable. A config is "receive-enabled"
    // exactly when imapHost is set. The password is encrypted at rest with the
    // same AES-256-GCM scheme as passwordEncrypted (see crypto.ts).
    imapHost: text("imap_host"),
    imapPort: integer("imap_port"),
    imapUsername: text("imap_username"),
    imapPasswordEncrypted: text("imap_password_encrypted"),
    imapSecure: boolean("imap_secure"),
    // Incremental-sync cursor. IMAP UIDs/UIDVALIDITY are 32-bit unsigned, which
    // overflows int4, so store as bigint. lastUid = highest UID we've ingested;
    // uidValidity guards against the server renumbering the mailbox (reset → 0).
    imapLastUid: bigint("imap_last_uid", { mode: "number" }),
    imapUidValidity: bigint("imap_uid_validity", { mode: "number" }),
    // Backfill cursor: lowest UID we've ingested; backfill walks below it until
    // imapBackfillComplete. Sync health surfaced in the UI.
    imapFirstUid: bigint("imap_first_uid", { mode: "number" }),
    imapBackfillComplete: boolean("imap_backfill_complete").notNull().default(false),
    imapLastSyncAt: timestamp("imap_last_sync_at"),
    imapLastSyncError: text("imap_last_sync_error"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    index("smtp_configs_organizationId_idx").on(table.organizationId),
    uniqueIndex("smtp_configs_one_default_per_org_uniq")
      .on(table.organizationId)
      .where(sql`is_default`),
  ],
);

export const apiTokens = pgTable(
  "api_tokens",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    userId: text("user_id").references(() => user.id, {
      onDelete: "set null",
    }),
    name: text("name").notNull(),
    role: text("role").$type<"admin" | "writer" | "reader">().default("writer").notNull(),
    tokenHash: text("token_hash").notNull().unique(),
    tokenPrefix: text("token_prefix").notNull(),
    smtpConfigId: uuid("smtp_config_id").references(() => smtpConfigs.id, {
      onDelete: "set null",
    }),
    lastUsedAt: timestamp("last_used_at"),
    expiresAt: timestamp("expires_at"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    index("api_tokens_organizationId_idx").on(table.organizationId),
    index("api_tokens_smtpConfigId_idx").on(table.smtpConfigId),
  ],
);

export const emailLogs = pgTable(
  "email_logs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id").references(() => organization.id, {
      onDelete: "cascade",
    }),
    smtpConfigId: uuid("smtp_config_id").references(() => smtpConfigs.id, {
      onDelete: "set null",
    }),
    userId: text("user_id").references(() => user.id, {
      onDelete: "set null",
    }),
    from: text("from").notNull(),
    to: jsonb("to").notNull().$type<{ email: string; name?: string }[]>(),
    subject: text("subject").notNull(),
    status: text("status").notNull(), // sent | deferred | failed | bounced | suppressed
    // The message body we composed (HTML + text), so the logs drawer can show
    // its content (previously only metadata was persisted). Stored WITHOUT
    // tracking artifacts — open/click pixels are injected only into the
    // transmitted copy — so this is the clean authored body, not the wire bytes.
    html: text("html"),
    text: text("text"),
    // RFC 5322 Message-Id of the message this one replies to (set when sending
    // a reply from the inbox), enabling proper threading on the recipient side.
    inReplyTo: text("in_reply_to"),
    // Caller-owned UUID echoed in terminal webhooks. It is intentionally
    // opaque and contains no recipient data.
    clientReference: text("client_reference"),
    clientType: text("client_type"),
    providerMessageId: text("provider_message_id"),
    error: text("error"),
    opens: integer("opens").notNull().default(0),
    openedAt: timestamp("opened_at"),
    clicks: integer("clicks").notNull().default(0),
    clickedAt: timestamp("clicked_at"),
    dkimSigned: boolean("dkim_signed").notNull().default(false),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    index("email_logs_createdAt_idx").on(table.createdAt),
    index("email_logs_organizationId_idx").on(table.organizationId),
  ],
);

// Received messages pulled over IMAP for a receive-enabled smtp_config. One row
// per IMAP message; (smtpConfigId, imapUid) is unique so re-polling is a no-op.
// Scoped to an organization for the same access model as everything else.
export const incomingEmails = pgTable(
  "incoming_emails",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    smtpConfigId: uuid("smtp_config_id")
      .notNull()
      .references(() => smtpConfigs.id, { onDelete: "cascade" }),
    imapUid: bigint("imap_uid", { mode: "number" }).notNull(),
    messageId: text("message_id"),
    inReplyTo: text("in_reply_to"),
    references: text("references"),
    // Deliberately allowlisted automation-safety headers. Do not store a raw
    // header map: these bounded values are sufficient for loop prevention while
    // avoiding unnecessary retention of arbitrary message metadata.
    autoSubmitted: varchar("auto_submitted", { length: 512 }),
    precedence: varchar("precedence", { length: 512 }),
    xAutoResponseSuppress: varchar("x_auto_response_suppress", { length: 512 }),
    fromAddress: text("from_address").notNull(),
    fromName: text("from_name"),
    toAddresses: jsonb("to_addresses")
      .notNull()
      .$type<{ email: string; name?: string }[]>()
      .default(sql`'[]'::jsonb`),
    ccAddresses: jsonb("cc_addresses").$type<
      { email: string; name?: string }[]
    >(),
    subject: text("subject"),
    snippet: text("snippet"),
    text: text("text"),
    html: text("html"),
    // RFC822.SIZE reported by IMAP, plus explicit truncation markers. Source
    // truncation means only the bounded prefix was parsed; content truncation
    // also covers individual fields trimmed before storage/API serialization.
    sourceSizeBytes: bigint("source_size_bytes", { mode: "number" }),
    sourceTruncated: boolean("source_truncated").notNull().default(false),
    contentTruncated: boolean("content_truncated").notNull().default(false),
    hasAttachments: boolean("has_attachments").notNull().default(false),
    attachments: jsonb("attachments").$type<
      { filename: string; contentType: string; size: number }[]
    >(),
    seen: boolean("seen").notNull().default(false),
    starred: boolean("starred").notNull().default(false),
    archived: boolean("archived").notNull().default(false),
    repliedAt: timestamp("replied_at"),
    receivedAt: timestamp("received_at").notNull(),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [
    uniqueIndex("incoming_emails_config_uid_uniq").on(
      table.smtpConfigId,
      table.imapUid,
    ),
    index("incoming_emails_organizationId_idx").on(table.organizationId),
    index("incoming_emails_config_received_idx").on(
      table.smtpConfigId,
      table.receivedAt,
    ),
  ],
);
