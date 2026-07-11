-- Audiences, contacts, and broadcasts
CREATE TABLE IF NOT EXISTS audiences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name text NOT NULL,
  created_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "audiences_organizationId_idx" ON audiences(organization_id);

CREATE TABLE IF NOT EXISTS contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  audience_id uuid NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
  email text NOT NULL,
  first_name text,
  last_name text,
  subscribed boolean NOT NULL DEFAULT true,
  unsubscribed_at timestamp,
  created_at timestamp DEFAULT now() NOT NULL,
  CONSTRAINT contacts_audience_email_uniq UNIQUE (audience_id, email)
);
CREATE INDEX IF NOT EXISTS "contacts_audienceId_idx" ON contacts(audience_id);

CREATE TABLE IF NOT EXISTS broadcasts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  audience_id uuid NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
  smtp_config_id uuid REFERENCES smtp_configs(id) ON DELETE SET NULL,
  template_id uuid,
  name text NOT NULL,
  subject text NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  total_count integer NOT NULL DEFAULT 0,
  sent_count integer NOT NULL DEFAULT 0,
  created_at timestamp DEFAULT now() NOT NULL,
  sent_at timestamp
);
CREATE INDEX IF NOT EXISTS "broadcasts_organizationId_idx" ON broadcasts(organization_id);
