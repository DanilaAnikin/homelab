-- Open/click tracking columns on email logs
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS opens integer NOT NULL DEFAULT 0;
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS opened_at timestamp;
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS clicks integer NOT NULL DEFAULT 0;
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS clicked_at timestamp;
