-- Base tables: everything 0001+ ALTERs or REFERENCES must already exist.
-- This migration bootstraps a blank database to the pre-organizations baseline
-- (Better Auth user/session/account/verification + mail smtp_configs/api_tokens/
-- email_logs + the legacy todos table). It is fully idempotent (CREATE ... IF NOT
-- EXISTS) so it is a no-op against a database that already has these tables.
-- Statements are separated by per-statement breakpoint markers so the runtime
-- migrator (src/migrate.ts) applies them one at a time.

-- 1. Better Auth: user --------------------------------------------------------
CREATE TABLE IF NOT EXISTS "user" (
  id text PRIMARY KEY,
  name text NOT NULL,
  email text NOT NULL UNIQUE,
  email_verified boolean DEFAULT false NOT NULL,
  image text,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint

-- 2. Better Auth: session -----------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
  id text PRIMARY KEY,
  expires_at timestamp NOT NULL,
  token text NOT NULL UNIQUE,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp NOT NULL,
  ip_address text,
  user_agent text,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  active_organization_id text
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "session_userId_idx" ON session(user_id);
--> statement-breakpoint

-- 3. Better Auth: account -----------------------------------------------------
CREATE TABLE IF NOT EXISTS account (
  id text PRIMARY KEY,
  account_id text NOT NULL,
  provider_id text NOT NULL,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  access_token text,
  refresh_token text,
  id_token text,
  access_token_expires_at timestamp,
  refresh_token_expires_at timestamp,
  scope text,
  password text,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "account_userId_idx" ON account(user_id);
--> statement-breakpoint

-- 4. Better Auth: verification ------------------------------------------------
CREATE TABLE IF NOT EXISTS verification (
  id text PRIMARY KEY,
  identifier text NOT NULL,
  value text NOT NULL,
  expires_at timestamp NOT NULL,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "verification_identifier_idx" ON verification(identifier);
--> statement-breakpoint

-- 5. Mail: smtp_configs -------------------------------------------------------
-- Created at the legacy (pre-organizations) shape: organization_id is added and
-- backfilled to NOT NULL by 0001. user_id is NOT NULL here and relaxed by 0001.
CREATE TABLE IF NOT EXISTS smtp_configs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  name text NOT NULL,
  host text NOT NULL,
  port integer NOT NULL DEFAULT 587,
  username text NOT NULL,
  password_encrypted text NOT NULL,
  from_address text NOT NULL,
  from_name text,
  is_default boolean NOT NULL DEFAULT false,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint

-- 6. Mail: api_tokens ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  name text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  token_prefix text NOT NULL,
  smtp_config_id uuid NOT NULL REFERENCES smtp_configs(id) ON DELETE SET NULL,
  last_used_at timestamp,
  expires_at timestamp,
  created_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "api_tokens_smtpConfigId_idx" ON api_tokens(smtp_config_id);
--> statement-breakpoint

-- 7. Mail: email_logs ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  smtp_config_id uuid REFERENCES smtp_configs(id) ON DELETE SET NULL,
  user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  "from" text NOT NULL,
  "to" jsonb NOT NULL,
  subject text NOT NULL,
  status text NOT NULL,
  provider_message_id text,
  error text,
  created_at timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "email_logs_createdAt_idx" ON email_logs(created_at);
--> statement-breakpoint

-- 8. Legacy todos table (kept in sync with schema.ts) -------------------------
CREATE TABLE IF NOT EXISTS todos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  completed boolean NOT NULL DEFAULT false,
  created_at timestamp DEFAULT now()
);
