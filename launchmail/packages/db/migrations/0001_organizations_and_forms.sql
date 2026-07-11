-- LaunchMail upgrade migration: organizations + roles + Formspree-style forms.
-- Idempotent and transactional — safe to run (and re-run) against an existing
-- pre-organizations database. Apply with: pnpm --filter @workspace/db db:upgrade
-- (or: psql "$DATABASE_URL" -f packages/db/migrations/0001_organizations_and_forms.sql)

BEGIN;

-- 1. Better Auth organization plugin tables ---------------------------------
CREATE TABLE IF NOT EXISTS organization (
  id text PRIMARY KEY,
  name text NOT NULL,
  slug text NOT NULL UNIQUE,
  logo text,
  metadata text,
  created_at timestamp DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS member (
  id text PRIMARY KEY,
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  role text NOT NULL DEFAULT 'member',
  created_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "member_organizationId_idx" ON member(organization_id);
CREATE INDEX IF NOT EXISTS "member_userId_idx" ON member(user_id);

CREATE TABLE IF NOT EXISTS invitation (
  id text PRIMARY KEY,
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  email text NOT NULL,
  role text,
  status text NOT NULL DEFAULT 'pending',
  expires_at timestamp NOT NULL,
  created_at timestamp DEFAULT now() NOT NULL,
  inviter_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE
);
ALTER TABLE invitation ADD COLUMN IF NOT EXISTS created_at timestamp DEFAULT now() NOT NULL;
CREATE INDEX IF NOT EXISTS "invitation_organizationId_idx" ON invitation(organization_id);
CREATE INDEX IF NOT EXISTS "invitation_email_idx" ON invitation(email);

-- 2. session.active_organization_id -----------------------------------------
ALTER TABLE session ADD COLUMN IF NOT EXISTS active_organization_id text;

-- 3. Org ownership columns on existing resources ----------------------------
ALTER TABLE smtp_configs ADD COLUMN IF NOT EXISTS organization_id text;
ALTER TABLE api_tokens   ADD COLUMN IF NOT EXISTS organization_id text;
ALTER TABLE api_tokens   ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'writer';
ALTER TABLE email_logs   ADD COLUMN IF NOT EXISTS organization_id text;

-- Relax legacy NOT NULLs (user_id becomes "created by"; tokens are no longer
-- bound to a single SMTP config).
ALTER TABLE smtp_configs ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE api_tokens   ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE api_tokens   ALTER COLUMN smtp_config_id DROP NOT NULL;

-- 4. Forms (Formspree-style) ------------------------------------------------
CREATE TABLE IF NOT EXISTS forms (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  smtp_config_id uuid REFERENCES smtp_configs(id) ON DELETE SET NULL,
  name text NOT NULL,
  slug text NOT NULL,
  endpoint_token text NOT NULL UNIQUE,
  template_key text NOT NULL DEFAULT 'contact',
  subject text,
  recipients jsonb NOT NULL,
  reply_to_field text,
  branding jsonb,
  custom_html text,
  settings jsonb,
  enabled boolean NOT NULL DEFAULT true,
  created_by_user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  created_at timestamp DEFAULT now() NOT NULL,
  updated_at timestamp DEFAULT now() NOT NULL
);
ALTER TABLE forms ADD COLUMN IF NOT EXISTS smtp_config_id uuid REFERENCES smtp_configs(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS "forms_organizationId_idx" ON forms(organization_id);
CREATE INDEX IF NOT EXISTS "forms_endpointToken_idx" ON forms(endpoint_token);

CREATE TABLE IF NOT EXISTS form_submissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  form_id uuid NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
  data jsonb NOT NULL,
  meta jsonb,
  email_status text NOT NULL DEFAULT 'queued',
  created_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "form_submissions_formId_idx" ON form_submissions(form_id);

-- 5. Backfill: a personal organization (admin) for every user without one ---
WITH to_create AS (
  SELECT u.id AS user_id, u.name, gen_random_uuid()::text AS org_id
  FROM "user" u
  WHERE NOT EXISTS (SELECT 1 FROM member m WHERE m.user_id = u.id)
), ins_org AS (
  INSERT INTO organization (id, name, slug, created_at)
  SELECT org_id,
         COALESCE(NULLIF(name, ''), 'My') || '''s Workspace',
         'ws-' || substr(org_id, 1, 8),
         now()
  FROM to_create
)
INSERT INTO member (id, organization_id, user_id, role, created_at)
SELECT gen_random_uuid()::text, org_id, user_id, 'admin', now()
FROM to_create;

-- 6. Link each user's existing resources to their organization --------------
UPDATE smtp_configs s SET organization_id = m.organization_id
FROM member m WHERE s.user_id = m.user_id AND s.organization_id IS NULL;
UPDATE api_tokens t SET organization_id = m.organization_id
FROM member m WHERE t.user_id = m.user_id AND t.organization_id IS NULL;
UPDATE email_logs e SET organization_id = m.organization_id
FROM member m WHERE e.user_id = m.user_id AND e.organization_id IS NULL;

-- 7. Foreign keys, indexes, and NOT NULL now that every row is linked -------
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'smtp_configs_organization_id_fkey') THEN
    ALTER TABLE smtp_configs ADD CONSTRAINT smtp_configs_organization_id_fkey
      FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'api_tokens_organization_id_fkey') THEN
    ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_organization_id_fkey
      FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'email_logs_organization_id_fkey') THEN
    ALTER TABLE email_logs ADD CONSTRAINT email_logs_organization_id_fkey
      FOREIGN KEY (organization_id) REFERENCES organization(id) ON DELETE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS "smtp_configs_organizationId_idx" ON smtp_configs(organization_id);
CREATE INDEX IF NOT EXISTS "api_tokens_organizationId_idx" ON api_tokens(organization_id);
CREATE INDEX IF NOT EXISTS "email_logs_organizationId_idx" ON email_logs(organization_id);

ALTER TABLE smtp_configs ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE api_tokens   ALTER COLUMN organization_id SET NOT NULL;

COMMIT;
