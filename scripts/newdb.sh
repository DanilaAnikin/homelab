#!/usr/bin/env bash
# ============================================================================
# newdb.sh — „Supabase zážitek": nová databáze pro projekt jedním příkazem.
#
#   ./newdb.sh <nazev>    vytvoří DB + uživatele + heslo, vypíše conn stringy
#   ./newdb.sh list       seznam všech databází
#
# Názvy: malá písmena, číslice, podtržítko; začíná písmenem. Max 30 znaků.
# ============================================================================
set -euo pipefail
PG_CONTAINER="shared-postgres"

psql_admin() { docker exec -i "$PG_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 "$@"; }

if [[ "${1:-}" == "list" ]]; then
  psql_admin -c "SELECT datname AS databaze, pg_size_pretty(pg_database_size(datname)) AS velikost
                 FROM pg_database WHERE NOT datistemplate ORDER BY 1;"
  exit 0
fi

NAME="${1:?Použití: ./newdb.sh <nazev_projektu> | list}"
[[ "$NAME" =~ ^[a-z][a-z0-9_]{1,29}$ ]] || {
  echo "✘ Neplatný název '$NAME' (povoleno: a-z, 0-9, _; začíná písmenem)"; exit 1; }

PASS=$(openssl rand -hex 16)

psql_admin <<SQL
CREATE ROLE ${NAME} LOGIN PASSWORD '${PASS}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE ${NAME} OWNER ${NAME};
REVOKE CONNECT ON DATABASE ${NAME} FROM PUBLIC;
SQL
psql_admin -d "$NAME" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null

cat <<EOF

✔ Databáze '${NAME}' vytvořena. ULOŽ SI HESLO — už se nikde nezobrazí.

  Pro aplikaci v Dokploy (pooled, běžný provoz):
    DATABASE_URL=postgres://${NAME}:${PASS}@shared-pgbouncer:6432/${NAME}

  Pro migrace (prisma migrate / drizzle push — přímé spojení):
    DIRECT_URL=postgres://${NAME}:${PASS}@shared-postgres:5432/${NAME}

  Z notebooku (nejdřív: ssh -L 5432:localhost:5432 anakin@homelab):
    postgres://${NAME}:${PASS}@localhost:5432/${NAME}
EOF
