-- Enforce at most one default SMTP config per organization. A partial unique
-- index only covers rows where is_default is true, so any number of non-default
-- configs are allowed while a second default insert/update fails.
--
-- First demote any pre-existing DUPLICATE defaults (legacy/production data may
-- have several rows with is_default = true in one organization). Keep the oldest
-- as the default per org and unset the rest, otherwise CREATE UNIQUE INDEX fails
-- with a duplicate-key error on existing data and aborts the migration.
UPDATE smtp_configs SET is_default = false
  WHERE is_default
    AND id NOT IN (
      SELECT DISTINCT ON (organization_id) id
        FROM smtp_configs
       WHERE is_default
       ORDER BY organization_id, created_at ASC, id ASC
    );
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS "smtp_configs_one_default_per_org_uniq"
  ON smtp_configs(organization_id)
  WHERE is_default;
