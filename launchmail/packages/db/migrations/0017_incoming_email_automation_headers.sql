-- Persist only the bounded automation-safety headers required by downstream
-- loop prevention. Arbitrary/raw RFC headers remain intentionally unstored.
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS auto_submitted varchar(512);
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS precedence varchar(512);
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS x_auto_response_suppress varchar(512);
