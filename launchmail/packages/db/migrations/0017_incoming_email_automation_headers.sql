-- Persist only the bounded automation-safety headers required by downstream
-- loop prevention. Arbitrary/raw RFC headers remain intentionally unstored.
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS auto_submitted varchar(512);
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS precedence varchar(512);
--> statement-breakpoint
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS x_auto_response_suppress varchar(512);
--> statement-breakpoint
-- NULL means the row predates the allowlisted-header parser (or parsing failed).
-- TRUE is written only by a successful source parse, even when all three
-- header values are legitimately absent.
ALTER TABLE incoming_emails ADD COLUMN IF NOT EXISTS automation_headers_complete boolean DEFAULT NULL;
