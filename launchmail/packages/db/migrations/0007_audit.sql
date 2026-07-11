-- Audit log of key write actions
CREATE TABLE IF NOT EXISTS audit_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  user_id text,
  user_name text,
  action text NOT NULL,
  target text,
  created_at timestamp DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS "audit_logs_organizationId_idx" ON audit_logs(organization_id);
