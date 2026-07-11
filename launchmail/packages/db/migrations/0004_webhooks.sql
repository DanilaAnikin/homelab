-- Webhooks: deliver email/form events to user-defined URLs (HMAC-signed)
CREATE TABLE IF NOT EXISTS webhooks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  url text NOT NULL,
  events jsonb NOT NULL,
  secret text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  last_status text,
  last_delivered_at timestamp,
  created_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "webhooks_organizationId_idx" ON webhooks(organization_id);
