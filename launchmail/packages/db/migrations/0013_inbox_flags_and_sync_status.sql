-- Inbox v2: message flags (star/archive), backfill cursor + sync health.

-- 1. Message flags for the inbox folders/actions.
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS starred boolean NOT NULL DEFAULT false;
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false;
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "incoming_emails_folder_idx" ON incoming_emails(smtp_config_id, archived, received_at);
--> statement-breakpoint

-- 2. Backfill cursor (walk UIDs below imap_first_uid) + per-mailbox sync health.
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_first_uid bigint;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_backfill_complete boolean NOT NULL DEFAULT false;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_last_sync_at timestamp;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_last_sync_error text;
