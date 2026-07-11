-- Email templates: user-built reusable templates (HTML or visual blocks)
CREATE TABLE IF NOT EXISTS email_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name text NOT NULL,
  mode text NOT NULL DEFAULT 'blocks',
  subject text,
  html text,
  blocks jsonb,
  accent text,
  theme text,
  created_by_user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "email_templates_organizationId_idx" ON email_templates(organization_id);
