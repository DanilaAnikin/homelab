-- ===========================================================================
-- TEST FIXTURE ONLY — not production schema, never applied to production.
--
-- storage.buckets / storage.objects are created by the storage-api service's
-- own migrations, not by supabase/postgres. The restore drill never needs
-- this file: it runs against a restored production database where these
-- tables already exist. It exists solely so the H-4 mutation suite can apply
-- repository migration 0006 and obtain a realistic RLS policy surface.
--
-- Columns mirror storage-api's shape closely enough for 0006 to apply; this
-- file is NOT a source of truth for storage schema fidelity.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS storage.buckets (
  id                 text PRIMARY KEY,
  name               text NOT NULL,
  owner              uuid,
  created_at         timestamptz DEFAULT now(),
  updated_at         timestamptz DEFAULT now(),
  public             boolean DEFAULT false,
  avif_autodetection boolean DEFAULT false,
  file_size_limit    bigint,
  allowed_mime_types text[],
  owner_id           text
);

CREATE TABLE IF NOT EXISTS storage.objects (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bucket_id        text REFERENCES storage.buckets(id),
  name             text,
  owner            uuid,
  created_at       timestamptz DEFAULT now(),
  updated_at       timestamptz DEFAULT now(),
  last_accessed_at timestamptz DEFAULT now(),
  metadata         jsonb,
  path_tokens      text[],
  version          text,
  owner_id         text,
  user_metadata    jsonb
);

ALTER TABLE storage.buckets ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
