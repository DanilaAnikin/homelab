-- Deliverability: suppression list + sending domains (SPF/DKIM/DMARC)
CREATE TABLE IF NOT EXISTS suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  email text NOT NULL,
  reason text NOT NULL DEFAULT 'manual',
  created_at timestamp DEFAULT now() NOT NULL,
  CONSTRAINT suppressions_org_email_uniq UNIQUE (organization_id, email)
);
CREATE INDEX IF NOT EXISTS "suppressions_organizationId_idx" ON suppressions(organization_id);

CREATE TABLE IF NOT EXISTS domains (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  domain text NOT NULL,
  dkim_selector text NOT NULL DEFAULT 'launchmail',
  dkim_public_key text,
  dkim_private_key text,
  spf_verified boolean NOT NULL DEFAULT false,
  dkim_verified boolean NOT NULL DEFAULT false,
  dmarc_verified boolean NOT NULL DEFAULT false,
  verified boolean NOT NULL DEFAULT false,
  last_checked_at timestamp,
  created_at timestamp DEFAULT now() NOT NULL,
  CONSTRAINT domains_org_domain_uniq UNIQUE (organization_id, domain)
);
CREATE INDEX IF NOT EXISTS "domains_organizationId_idx" ON domains(organization_id);
