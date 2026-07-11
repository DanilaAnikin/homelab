-- forms.template_id and broadcasts.template_id were bare uuid columns with no FK.
-- Add ON DELETE SET NULL foreign keys to email_templates so deleting a template
-- nulls the reference instead of orphaning it. Guarded so re-runs are no-ops.
--
-- First null out any pre-existing ORPHANED references (a template_id pointing at
-- a row that no longer exists in email_templates). Legacy/production data can
-- contain these, and without the cleanup ADD CONSTRAINT fails with a 23503
-- foreign-key violation, aborting the whole migration (and app startup).
UPDATE forms SET template_id = NULL
  WHERE template_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM email_templates t WHERE t.id = forms.template_id);
--> statement-breakpoint
UPDATE broadcasts SET template_id = NULL
  WHERE template_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM email_templates t WHERE t.id = broadcasts.template_id);
--> statement-breakpoint
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'forms_template_id_email_templates_id_fk') THEN
    ALTER TABLE forms ADD CONSTRAINT forms_template_id_email_templates_id_fk
      FOREIGN KEY (template_id) REFERENCES email_templates(id) ON DELETE SET NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'broadcasts_template_id_email_templates_id_fk') THEN
    ALTER TABLE broadcasts ADD CONSTRAINT broadcasts_template_id_email_templates_id_fk
      FOREIGN KEY (template_id) REFERENCES email_templates(id) ON DELETE SET NULL;
  END IF;
END $$;
