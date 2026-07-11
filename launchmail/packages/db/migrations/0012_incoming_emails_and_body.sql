-- Incoming mail (IMAP) + full sent-message bodies + reply threading.
--
-- 1. smtp_configs gains optional IMAP fields so a connection can also RECEIVE.
--    A config is "receive-enabled" exactly when imap_host is set. The IMAP
--    password is encrypted at rest with the same scheme as password_encrypted.
--    imap_last_uid / imap_uid_validity are the incremental-sync cursor.
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_host text;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_port integer;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_username text;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_password_encrypted text;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_secure boolean;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_last_uid bigint;
--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS imap_uid_validity bigint;
--> statement-breakpoint

-- 2. email_logs stores the rendered body of what we sent (so the logs drawer can
--    show the actual message) and the In-Reply-To id when a row is a reply.
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS html text;
--> statement-breakpoint
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS "text" text;
--> statement-breakpoint
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS in_reply_to text;
--> statement-breakpoint

-- 3. incoming_emails: one row per IMAP message. (smtp_config_id, imap_uid) is
--    unique so re-polling the same mailbox is an idempotent no-op.
CREATE TABLE IF NOT EXISTS incoming_emails (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  smtp_config_id uuid NOT NULL REFERENCES smtp_configs(id) ON DELETE CASCADE,
  imap_uid bigint NOT NULL,
  message_id text,
  in_reply_to text,
  "references" text,
  from_address text NOT NULL,
  from_name text,
  to_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
  cc_addresses jsonb,
  subject text,
  snippet text,
  "text" text,
  html text,
  has_attachments boolean NOT NULL DEFAULT false,
  attachments jsonb,
  seen boolean NOT NULL DEFAULT false,
  replied_at timestamp,
  received_at timestamp NOT NULL,
  created_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS "incoming_emails_config_uid_uniq" ON incoming_emails(smtp_config_id, imap_uid);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "incoming_emails_organizationId_idx" ON incoming_emails(organization_id);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "incoming_emails_config_received_idx" ON incoming_emails(smtp_config_id, received_at);
