#!/bin/bash
# Spustí se JEDNOU při prvním initu databáze (docker-entrypoint-initdb.d).
# Vytvoří auth mechanismus pro PgBouncer (auth_query přes SECURITY DEFINER
# funkci) — díky tomu PgBouncer zvládne KAŽDÉHO nového uživatele z newdb.sh
# automaticky, bez zásahu do userlist.txt.
set -e

psql -v ON_ERROR_STOP=1 -U postgres <<SQL
CREATE ROLE pgbouncer LOGIN PASSWORD '${PGBOUNCER_PASSWORD}';
CREATE SCHEMA pgbouncer AUTHORIZATION pgbouncer;

CREATE OR REPLACE FUNCTION pgbouncer.get_auth(p_usename TEXT)
RETURNS TABLE(username TEXT, password TEXT)
LANGUAGE sql SECURITY DEFINER AS \$\$
  SELECT usename::TEXT, passwd::TEXT
  FROM pg_catalog.pg_shadow
  WHERE usename = p_usename;
\$\$;

REVOKE ALL ON FUNCTION pgbouncer.get_auth(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pgbouncer.get_auth(TEXT) TO pgbouncer;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL
