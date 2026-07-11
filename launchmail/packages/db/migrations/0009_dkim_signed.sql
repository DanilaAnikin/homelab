-- Record whether an email was DKIM-signed by a verified sending domain
ALTER TABLE email_logs ADD COLUMN IF NOT EXISTS dkim_signed boolean NOT NULL DEFAULT false;
