-- Forms can use a user-built email template (email_templates) instead of a built-in design
ALTER TABLE forms ADD COLUMN IF NOT EXISTS template_id uuid;
