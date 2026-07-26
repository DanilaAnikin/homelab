#!/usr/bin/env bash
# Ripieno — migrace dat ze Supabase do homelab ripieno-postgres (Fáze 8, strategie B).
#
# Spouští se AŽ PŘI CUTOVERU (Fáze 10), po tom co:
#   - schéma (0000_strategy_b_init.sql + 0001_functions.sql) je aplikované v DB `ripieno`
#   - Ripieno je na Supabase krátce ZMRAŽENÉ (žádné nové zápisy během dumpu)
#
# Žádné secrety nejsou zabudované — vše přes env proměnné:
#   SOURCE_URL          postgres://…  (hosted Supabase, z ~/programming/ripieno/.env → SUPABASE_DB_URL)
#   DEST_SUPERUSER_URL  postgres://postgres:<POSTGRES_PASSWORD>@localhost:5432/ripieno
#                       (superuser kvůli `session_replication_role=replica` při restore)
#
# Použití (na homelabu, uvnitř sítě k shared-postgres):
#   SOURCE_URL='…' DEST_SUPERUSER_URL='…' bash ripieno-data-migration.sh
set -euo pipefail

: "${SOURCE_URL:?SOURCE_URL (Supabase) není nastaveno}"
: "${DEST_SUPERUSER_URL:?DEST_SUPERUSER_URL (ripieno-postgres superuser) není nastaveno}"

WORK="$(mktemp -d)"
DUMP="$WORK/ripieno-public-data.sql"
trap 'rm -rf "$WORK"' EXIT

echo "▶ 1/4  Dump dat ze Supabase (jen public schéma, data-only)…"
# Supabase public schéma = původní app tabulky. Better Auth (user/session/account/
# verification), run_seq a stripe_processed_events v Supabase NEEXISTUJÍ (jsou nové
# ve strategii B) → dump je neobsahuje automaticky. auth.*/storage.* jsou mimo public.
pg_dump "$SOURCE_URL" \
  --data-only --no-owner --no-privileges \
  --schema=public \
  --disable-triggers \
  -f "$DUMP"
echo "   dump: $(wc -l < "$DUMP") řádků → $DUMP"

echo "▶ 2/4  Restore do ripieno-postgres (FK triggery vypnuté po dobu loadu)…"
# session_replication_role=replica vypne FK/uživatelské triggery → nezáleží na pořadí
# tabulek v dumpu. Vyžaduje superuser (proto DEST_SUPERUSER_URL). Celé v transakci.
psql "$DEST_SUPERUSER_URL" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SET session_replication_role = replica;
\i $DUMP
SET session_replication_role = origin;
COMMIT;
SQL
echo "   restore OK"

echo "▶ 3/4  Seed Better Auth uživatelů z app tabulky users (stejné UUID)…"
# Klíčové: Better Auth user.id MUSÍ = app users.id (= původní Supabase auth uuid),
# aby se po magic-link loginu founder namapoval na svoji existující org/runs/data.
# Magic-link nepotřebuje řádky v account (jen user + verification za běhu).
psql "$DEST_SUPERUSER_URL" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO "user" (id, name, email, email_verified, created_at, updated_at)
SELECT u.id,
       COALESCE(NULLIF(u.name, ''), split_part(u.email, '@', 1)),
       u.email,
       true,
       now(),
       now()
FROM users u
ON CONFLICT (id) DO NOTHING;
SQL
echo "   founder(s) seed OK"

echo "▶ 4/4  Přepočet run_seq (max seq per run) + kontrola integrity…"
psql "$DEST_SUPERUSER_URL" -v ON_ERROR_STOP=1 <<'SQL'
-- run_seq se nemigroval; nastav na aktuální max(seq) per run, ať budoucí eventy navazují.
INSERT INTO run_seq (run_id, value)
SELECT run_id, MAX(seq) FROM events GROUP BY run_id
ON CONFLICT (run_id) DO UPDATE SET value = EXCLUDED.value;

-- Kontrola: počty klíčových tabulek (porovnej vizuálně se Supabase).
SELECT 'organizations' t, count(*) FROM organizations
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'user (better-auth)', count(*) FROM "user"
UNION ALL SELECT 'memberships', count(*) FROM memberships
UNION ALL SELECT 'projects', count(*) FROM projects
UNION ALL SELECT 'runs', count(*) FROM runs
UNION ALL SELECT 'agents', count(*) FROM agents
UNION ALL SELECT 'events', count(*) FROM events
UNION ALL SELECT 'messages', count(*) FROM messages
UNION ALL SELECT 'credit_wallet', count(*) FROM credit_wallet
ORDER BY t;

-- Integrita: každá membership musí mít existující usera i org (0 = OK).
SELECT 'orphan memberships' issue, count(*) FROM memberships m
  LEFT JOIN users u ON u.id = m.user_id
  LEFT JOIN organizations o ON o.id = m.organization_id
  WHERE u.id IS NULL OR o.id IS NULL;
SQL

echo "✅ Hotovo. Zkontroluj počty výše proti Supabase; pak lze pustit login test."
