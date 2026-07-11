-- Direct MX delivery: an smtp_config can now deliver mail itself (resolve the
-- recipient's MX and talk SMTP on port 25) instead of relaying through an
-- upstream smarthost. Direct configs carry no upstream credentials, so the
-- credential columns become nullable and two new columns are added.
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS type text NOT NULL DEFAULT 'smarthost';--> statement-breakpoint
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS helo_hostname text;--> statement-breakpoint
ALTER TABLE smtp_configs ALTER COLUMN host DROP NOT NULL;--> statement-breakpoint
ALTER TABLE smtp_configs ALTER COLUMN username DROP NOT NULL;--> statement-breakpoint
ALTER TABLE smtp_configs ALTER COLUMN password_encrypted DROP NOT NULL;
