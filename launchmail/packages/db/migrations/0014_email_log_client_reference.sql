ALTER TABLE "email_logs"
  ADD COLUMN IF NOT EXISTS "client_reference" text,
  ADD COLUMN IF NOT EXISTS "client_type" text;

CREATE INDEX IF NOT EXISTS "email_logs_client_reference_idx"
  ON "email_logs" ("client_reference")
  WHERE "client_reference" IS NOT NULL;

CREATE INDEX IF NOT EXISTS "email_logs_client_type_idx"
  ON "email_logs" ("client_type")
  WHERE "client_type" IS NOT NULL;
