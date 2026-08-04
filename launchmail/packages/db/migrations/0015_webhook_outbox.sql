-- Transactional webhook outbox. Domain state and webhook notification intent
-- are committed together; a BullMQ relay delivers rows asynchronously.
CREATE TABLE IF NOT EXISTS "webhook_outbox" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "organization_id" text NOT NULL REFERENCES "organization"("id") ON DELETE CASCADE,
  "hook_id" uuid REFERENCES "webhooks"("id") ON DELETE SET NULL,
  "event" text NOT NULL,
  "data" jsonb NOT NULL,
  "occurred_at" timestamptz NOT NULL,
  "idempotency_key" text NOT NULL UNIQUE,
  "queued_at" timestamp,
  "completed_at" timestamp,
  "failed_at" timestamp,
  "last_error" text,
  "created_at" timestamp DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS "webhook_outbox_pending_idx"
  ON "webhook_outbox" ("queued_at", "failed_at", "created_at");
CREATE INDEX IF NOT EXISTS "webhook_outbox_hook_idx"
  ON "webhook_outbox" ("hook_id");
