-- Record when IMAP ingestion intentionally kept only a bounded prefix/body.
-- Existing rows have unknown source size but remain readable; API-side limits
-- independently bound their detail responses.
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS source_size_bytes bigint;
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS source_truncated boolean NOT NULL DEFAULT false;
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS content_truncated boolean NOT NULL DEFAULT false;
