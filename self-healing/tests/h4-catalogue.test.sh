#!/usr/bin/env bash
# ============================================================================
# H-4 mutation suite — "the schema fingerprint is not a schema fingerprint"
#
# Builds a real PostgreSQL 17.6 reference by applying the repository's
# migrations 0001-0023, captures a baseline, then mutates ONE security-relevant
# property at a time and asks each fingerprint implementation whether anything
# changed.
#
#   legacy    sha256(nspname.relname:relkind:owner)   — the audited version
#   canonical lib/nt-catalogue.sql                    — the replacement
#
# A mutation is REPORTED for an implementation when that implementation's
# fingerprint changes. The headline claim under test is the audit's H-4:
# dropping every RLS policy leaves the legacy fingerprint byte-identical.
#
# Env:
#   NT_MIGRATIONS  directory holding 0001..0023 (default: nate_trader repo)
#   NT_PG_IMAGE    default supabase/postgres:17.6.1.136
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CATSQL="$HERE/../lib/nt-catalogue.sql"
MIGDIR="${NT_MIGRATIONS:-/home/anakin/programming/nate_trader/supabase/migrations}"
IMAGE="${NT_PG_IMAGE:-supabase/postgres:17.6.1.136}"
CN="ntv-h4-$$"
WORK="$(mktemp -d)"
REF=ntref

TOTAL=0; LEGACY_BLIND=0; CANON_CAUGHT=0
declare -a BLIND_LIST=()

cleanup(){ docker rm -f "$CN" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

psqlx(){ docker exec "$CN" psql -U supabase_admin -d "$1" -X -A -t -q -v ON_ERROR_STOP=1 -c "$2"; }

# The audited fingerprint, reproduced verbatim from nt-restore-drill.sh:238.
legacy_fp(){
  docker exec "$CN" psql -U supabase_admin -d "$1" -X -A -t -q -c \
    "SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (SELECT n.nspname||'.'||c.relname||':'||c.relkind::text||':'||pg_get_userbyid(c.relowner)::text AS x FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname<>'information_schema') s"
}
# One salt for the whole run. The catalogue now REQUIRES ntv_pw_salt, because a
# fixed domain separator over rolpassword is a deterministic, offline-attackable
# commitment to every role's password hash. Within one mutation suite the salt
# only has to be constant, since every fingerprint is compared to the baseline
# taken in the same run.
H4_SALT="h4-fixed-salt-for-one-run"
canonical_fp(){
  docker exec "$CN" psql -U supabase_admin -d "$1" -X -v "ntv_pw_salt=$H4_SALT" \
    -f /tmp/nt-catalogue.sql | sha256sum | cut -d' ' -f1
}

# ── bring up the reference ──────────────────────────────────────────────────
echo "H-4 mutation suite   image=$IMAGE"
docker rm -f "$CN" >/dev/null 2>&1
docker run -d --name "$CN" --network none -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" "$IMAGE" >/dev/null
for _ in $(seq 1 120); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$CN" 2>/dev/null)" = healthy ] && break; sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$CN")" = healthy ] || { echo "FATAL: $IMAGE never became healthy"; exit 1; }
docker cp "$CATSQL" "$CN:/tmp/nt-catalogue.sql" >/dev/null

# The reference must inherit the image's Supabase surface (schema auth,
# auth.users, the vault, the standard roles) — migration 0002 references
# auth.uid() and auth.users, so a bare template1 database cannot host them.
# `postgres` is held open by the pg_cron and pg_net background workers, so
# connections are blocked for the duration of the copy rather than raced.
clone_db(){ # clone_db <source> <target>
  docker exec -i "$CN" psql -U supabase_admin -d template1 -X -A -t -q -v ON_ERROR_STOP=1 <<SQL >/dev/null
ALTER DATABASE $1 WITH ALLOW_CONNECTIONS false;
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$1' AND pid<>pg_backend_pid();
CREATE DATABASE $2 TEMPLATE $1 OWNER supabase_admin;
ALTER DATABASE $1 WITH ALLOW_CONNECTIONS true;
SQL
}
clone_db postgres "$REF"

# stand-in for the storage-api service's own migrations (see fixture header)
docker cp "$HERE/fixtures/storage-surface.sql" "$CN:/tmp/storage.sql" >/dev/null
docker exec "$CN" psql -U supabase_admin -d "$REF" -X -q -v ON_ERROR_STOP=1 -f /tmp/storage.sql >/dev/null \
  || { echo "FATAL: storage fixture failed to apply"; exit 1; }

APPLIED=0
for m in $(ls "$MIGDIR"/[0-9][0-9][0-9][0-9]_*.sql | sort); do
  docker cp "$m" "$CN:/tmp/m.sql" >/dev/null
  if ! docker exec "$CN" psql -U supabase_admin -d "$REF" -X -q -v ON_ERROR_STOP=1 -f /tmp/m.sql \
        >"$WORK/mig.out" 2>"$WORK/mig.err"; then
    echo "FATAL: migration $(basename "$m") failed to apply:"; tail -15 "$WORK/mig.err"; exit 1
  fi
  APPLIED=$((APPLIED+1))
done
echo "applied $APPLIED migrations into $REF"
POLICIES="$(psqlx "$REF" "SELECT count(*) FROM pg_policies WHERE schemaname NOT LIKE 'pg\\_%'")"
echo "reference has $POLICIES RLS policies"
[ "$POLICIES" -gt 0 ] || { echo "FATAL: reference has no RLS policies — the suite would prove nothing"; exit 1; }

BASE_LEGACY="$(legacy_fp "$REF")"
BASE_CANON="$(canonical_fp "$REF")"
echo "baseline legacy   = $BASE_LEGACY"
echo "baseline canonical= $BASE_CANON"
echo

# ── mutation driver ─────────────────────────────────────────────────────────
# Each mutation runs on a fresh copy of the reference so mutations cannot
# interact. TEMPLATE copy is exact, including policies, ACLs and defaults.
mutate(){ # mutate <name> <sql>
  local name="$1" sql="$2" db="mut$TOTAL" lfp cfp lch cch
  TOTAL=$((TOTAL+1))
  clone_db "$REF" "$db" 2>/dev/null || {
    printf '  ERROR %-46s could not clone reference\n' "$name"; return; }
  if ! docker exec "$CN" psql -U supabase_admin -d "$db" -X -q -v ON_ERROR_STOP=1 -c "$sql" \
        >/dev/null 2>"$WORK/mut.err"; then
    printf '  ERROR %-46s mutation SQL failed: %s\n' "$name" "$(head -1 "$WORK/mut.err")"
    psqlx postgres "DROP DATABASE $db" >/dev/null 2>&1; return
  fi
  lfp="$(legacy_fp "$db")"; cfp="$(canonical_fp "$db")"
  [ "$lfp" != "$BASE_LEGACY" ] && lch=DETECTED || lch=BLIND
  [ "$cfp" != "$BASE_CANON" ]  && cch=DETECTED || cch=BLIND
  [ "$cch" = DETECTED ] && CANON_CAUGHT=$((CANON_CAUGHT+1))
  if [ "$lch" = BLIND ]; then LEGACY_BLIND=$((LEGACY_BLIND+1)); BLIND_LIST+=("$name"); fi
  printf '  %-46s legacy=%-8s canonical=%s\n' "$name" "$lch" "$cch"
  psqlx postgres "DROP DATABASE $db" >/dev/null 2>&1
}

echo "mutation                                        legacy fingerprint / canonical catalogue"
echo "------------------------------------------------------------------------------------"

# the headline: a restored database that lost every policy
mutate "drop EVERY RLS policy" \
  "DO \$\$ DECLARE r record; BEGIN FOR r IN SELECT schemaname,tablename,policyname FROM pg_policies WHERE schemaname='public' LOOP EXECUTE format('DROP POLICY %I ON %I.%I', r.policyname, r.schemaname, r.tablename); END LOOP; END \$\$;"
mutate "drop ONE RLS policy" \
  "DO \$\$ DECLARE r record; BEGIN SELECT schemaname,tablename,policyname INTO r FROM pg_policies WHERE schemaname='public' ORDER BY 1,2,3 LIMIT 1; EXECUTE format('DROP POLICY %I ON %I.%I', r.policyname, r.schemaname, r.tablename); END \$\$;"
mutate "disable RLS on a table" \
  "DO \$\$ DECLARE t text; BEGIN SELECT c.relname INTO t FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relrowsecurity ORDER BY 1 LIMIT 1; EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t); END \$\$;"
mutate "change a policy USING expression" \
  "DO \$\$ DECLARE r record; BEGIN SELECT schemaname,tablename,policyname INTO r FROM pg_policies WHERE schemaname='public' AND qual IS NOT NULL ORDER BY 1,2,3 LIMIT 1; EXECUTE format('ALTER POLICY %I ON %I.%I USING (true)', r.policyname, r.schemaname, r.tablename); END \$\$;"
mutate "change a column type" \
  "DO \$\$ DECLARE r record; BEGIN SELECT c.relname tb, a.attname col INTO r FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped AND format_type(a.atttypid,a.atttypmod)='text' ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I TYPE varchar(255)', r.tb, r.col); END \$\$;"
mutate "remove a column default" \
  "DO \$\$ DECLARE r record; BEGIN SELECT c.relname tb, a.attname col INTO r FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I DROP DEFAULT', r.tb, r.col); END \$\$;"
mutate "drop a CHECK constraint" \
  "DO \$\$ DECLARE r record; BEGIN SELECT c.relname tb, con.conname cn INTO r FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND con.contype='c' ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', r.tb, r.cn); END \$\$;"
mutate "mark a constraint NOT VALID" \
  "DO \$\$ DECLARE r record; BEGIN SELECT c.relname tb, con.conname cn, pg_get_constraintdef(con.oid) d INTO r FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND con.contype='c' ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', r.tb, r.cn); EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I %s NOT VALID', r.tb, r.cn, r.d); END \$\$;"
mutate "drop a non-constraint index" \
  "DO \$\$ DECLARE r text; BEGIN SELECT ic.relname INTO r FROM pg_index ix JOIN pg_class ic ON ic.oid=ix.indexrelid JOIN pg_class c ON c.oid=ix.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT ix.indisprimary AND NOT ix.indisunique ORDER BY 1 LIMIT 1; EXECUTE format('DROP INDEX public.%I', r); END \$\$;"
mutate "disable a trigger" \
  "DO \$\$ DECLARE r record; BEGIN SELECT c.relname tb, t.tgname tg INTO r FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER TABLE public.%I DISABLE TRIGGER %I', r.tb, r.tg); END \$\$;"
mutate "flip a function to SECURITY INVOKER" \
  "DO \$\$ DECLARE r record; BEGIN SELECT p.proname nm, pg_get_function_identity_arguments(p.oid) ar INTO r FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER FUNCTION public.%I(%s) SECURITY INVOKER', r.nm, r.ar); END \$\$;"
mutate "change a function search_path" \
  "DO \$\$ DECLARE r record; BEGIN SELECT p.proname nm, pg_get_function_identity_arguments(p.oid) ar INTO r FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proconfig IS NOT NULL ORDER BY 1,2 LIMIT 1; EXECUTE format('ALTER FUNCTION public.%I(%s) SET search_path = public, pg_temp', r.nm, r.ar); END \$\$;"
mutate "revoke a table ACL from authenticated" \
  "DO \$\$ DECLARE t text; BEGIN SELECT c.relname INTO t FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relacl IS NOT NULL ORDER BY 1 LIMIT 1; EXECUTE format('REVOKE ALL ON public.%I FROM authenticated', t); END \$\$;"
mutate "grant an excessive ACL to anon" \
  "DO \$\$ DECLARE t text; BEGIN SELECT c.relname INTO t FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1 LIMIT 1; EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON public.%I TO anon', t); END \$\$;"
mutate "alter a default privilege" \
  "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anon;"
mutate "change a role membership" \
  "GRANT authenticated TO anon;"
mutate "change a sequence value" \
  "DO \$\$ DECLARE s text; BEGIN SELECT format('%I.%I',schemaname,sequencename) INTO s FROM pg_sequences WHERE schemaname='public' ORDER BY 1 LIMIT 1; IF s IS NOT NULL THEN EXECUTE format('SELECT setval(%L, 987654)', s); END IF; END \$\$;"
mutate "drop a routine" \
  "DO \$\$ DECLARE r record; BEGIN SELECT p.proname nm, pg_get_function_identity_arguments(p.oid) ar INTO r FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f' ORDER BY 1,2 LIMIT 1; EXECUTE format('DROP FUNCTION public.%I(%s) CASCADE', r.nm, r.ar); END \$\$;"

echo
echo "totals: $TOTAL mutations; legacy blind to $LEGACY_BLIND; canonical detected $CANON_CAUGHT"
if [ ${#BLIND_LIST[@]} -gt 0 ]; then
  echo "legacy fingerprint was BLIND to:"
  for b in "${BLIND_LIST[@]}"; do echo "  - $b"; done
fi
echo
if [ "$CANON_CAUGHT" -ne "$TOTAL" ]; then
  echo "H-4 SUITE RED: canonical catalogue missed $((TOTAL-CANON_CAUGHT)) mutation(s)"; exit 1
fi
echo "H-4 SUITE GREEN: canonical catalogue detected all $TOTAL mutations"
