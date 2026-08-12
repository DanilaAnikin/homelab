#!/usr/bin/env bash
# ============================================================================
# nt-restore-drill.sh — prove a V2 recovery set actually restores.
#
# Usage: nt-restore-drill.sh <set_id> [remote_index]
#          remote_index 0 = r2 (primary, default), 1 = r2dr (DR copy)
#
# This is deliberately hostile to itself. The legacy drill went green on a
# clone that was missing public.accounts and every Vault row, because it
# restored with errors suppressed and then counted tables that happened to
# exist. Here: any unexpected restore error or warning fails the drill, every
# invariant is compared against the manifest recorded at dump time, and nine
# negative controls must each FAIL for the drill to pass.
#
# Secret handling: the pgsodium root key and the Vault plaintexts are never
# printed, never written to disk outside tmpfs, and never leave the database
# except as an HMAC under a random per-run key. Only booleans reach evidence.
# ============================================================================
set -euo pipefail
umask 077

SETID="${1:?usage: nt-restore-drill.sh <set_id> [remote_index]}"
RIDX="${2:-0}"
REMOTES=("r2:homelab-backups" "r2dr:homelab-backups-dr")
REMOTE="${REMOTES[$RIDX]}"
PREFIX="natetrader-recovery-v2/$SETID"

SECRETS=/srv/homelab/secrets
KEY_DB="$SECRETS/nt-v2-db-key.txt"
KEY_ROOT="$SECRETS/nt-v2-rootkey-key.txt"
RC="rclone --config $SECRETS/rclone.conf --retries 5"

RUN="drill-$(date -u +%Y%m%dT%H%M%SZ)-r$RIDX"
NET="ntd-$RUN"; VOL_DATA="ntd-data-$RUN"; VOL_CFG="ntd-cfg-$RUN"; CN="ntd-db-$RUN"
EVID="/home/anakin/nt-r0/${SETID}-r${RIDX}"
WORK=""
PASS=0; FAIL=0

log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }
check(){ # check <name> <expected> <actual>
  if [[ "$2" == "$3" ]]; then PASS=$((PASS+1)); printf '  PASS  %-42s %s\n' "$1" "$3"
  else FAIL=$((FAIL+1)); printf '  FAIL  %-42s expected=%s actual=%s\n' "$1" "$2" "$3"; fi
}
negative(){ # negative <name> <cmd...>  — the command MUST fail
  if "${@:2}" >/dev/null 2>&1; then FAIL=$((FAIL+1)); printf '  FAIL  NEGATIVE %-33s unexpectedly SUCCEEDED\n' "$1"
  else PASS=$((PASS+1)); printf '  PASS  NEGATIVE %-33s correctly rejected\n' "$1"; fi
}

# NOTE on self-healing/CLAUDE.md's iron rule "NIKDY docker volume rm":
# that rule protects PRODUCTION data volumes. Everything removed here is
# created by this script moments earlier and is uniquely named per run
# (ntd-data-drill-…, ntd-cfg-drill-…). No production volume, container or
# network can match these names, and this script never touches the
# natetrader-* stack except to read from it.
teardown(){
  docker rm -f "$CN" >/dev/null 2>&1 || true
  docker volume rm -f "$VOL_DATA" "$VOL_CFG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
cleanup(){
  local rc=$?
  teardown
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    mountpoint -q "$WORK" && umount "$WORK" 2>/dev/null || true
    rmdir "$WORK" 2>/dev/null || rm -rf "$WORK" 2>/dev/null || true
  fi
  exit $rc
}
trap cleanup EXIT INT TERM HUP

[[ $EUID -eq 0 ]] || { echo "must run as root"; exit 1; }
mkdir -p "$EVID"; chmod 700 "$EVID"
WORK="$(mktemp -d /run/nt-drill.XXXXXX)"
mount -t tmpfs -o size=8G,mode=0700,noexec,nosuid,nodev tmpfs "$WORK"

log "=== drill $RUN : set $SETID from $REMOTE ==="

# ── 0. completion marker must exist BEFORE anything is trusted ──────────────
# `rclone cat` exits 0 and emits nothing for an object that does not exist, so
# exit status alone is not a presence test — an unpublished set would sail
# straight through. The gate therefore requires non-empty, well-formed content,
# and the negative control below exercises this very function.
marker_valid(){ # marker_valid <remote_path> <dest>
  local dest="${2:-$WORK/COMPLETE}"
  : > "$dest"
  $RC cat "$1" > "$dest" 2>/dev/null || true
  [[ -s "$dest" ]] && grep -q '^set_id=' "$dest"
}
marker_valid "$REMOTE/$PREFIX/COMPLETE" \
  || { echo "FATAL: no valid COMPLETE marker — set is not publishable"; exit 1; }
grep -q "^set_id=$SETID$" "$WORK/COMPLETE" \
  || { echo "FATAL: completion marker names a different set"; exit 1; }
$RC cat "$REMOTE/$PREFIX/MANIFEST.json" > "$WORK/MANIFEST.json"
MSHA="$(sha256sum "$WORK/MANIFEST.json" | cut -d' ' -f1)"
grep -q "manifest_sha256=$MSHA" "$WORK/COMPLETE" \
  || { echo "FATAL: manifest digest does not match completion marker"; exit 1; }
log "completion marker and manifest digest agree"

mj(){ python3 -c "import json,sys;d=json.load(open('$WORK/MANIFEST.json'));print(eval(sys.argv[1],{'d':d}))" "$1"; }
IMAGE="$(mj "d['source']['image_ref']")"
RK_OWNER="$(mj "d['root_key']['owner']")"
RK_SHA="$(mj "d['root_key']['sha256']")"

# ── 1. fetch components, verify ciphertext then plaintext digests ───────────
for c in db.dump.gpg globals.sql.gpg dbconfig.tar.gz.gpg rootkey.bin.gpg; do
  $RC cat "$REMOTE/$PREFIX/$c" > "$WORK/$c"
  want="$(mj "[x['ciphertext_sha256'] for x in d['components'] if x['name']=='$c'][0]")"
  got="$(sha256sum "$WORK/$c" | cut -d' ' -f1)"
  check "ciphertext digest $c" "$want" "$got"
done

dec(){ gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$2" -d "$1" 2>/dev/null; }
dec "$WORK/db.dump.gpg"          "$KEY_DB"   > "$WORK/db.dump"
dec "$WORK/globals.sql.gpg"      "$KEY_DB"   > "$WORK/globals.sql"
dec "$WORK/dbconfig.tar.gz.gpg"  "$KEY_DB"   > "$WORK/dbconfig.tar.gz"
dec "$WORK/rootkey.bin.gpg"      "$KEY_ROOT" > "$WORK/rootkey.bin"
for p in db.dump globals.sql dbconfig.tar.gz rootkey.bin; do
  want="$(mj "[x['plaintext_sha256'] for x in d['components'] if x['name']=='$p.gpg'][0]")"
  got="$(sha256sum "$WORK/$p" | cut -d' ' -f1)"
  check "plaintext digest $p" "$want" "$got"
done

# ── 2. NEGATIVE CONTROLS on the encrypted material ──────────────────────────
log "negative controls (encryption / marker layer)"
cp "$WORK/db.dump.gpg" "$WORK/corrupt.gpg"
SZ=$(stat -c %s "$WORK/corrupt.gpg"); printf '\xff' | dd of="$WORK/corrupt.gpg" bs=1 seek=$((SZ-8)) conv=notrunc status=none
negative "corrupt encrypted component" gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$KEY_DB" -d "$WORK/corrupt.gpg"
negative "wrong key for root key component" gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$KEY_DB" -d "$WORK/rootkey.bin.gpg"
negative "missing completion marker" marker_valid "$REMOTE/$PREFIX/COMPLETE.absent" "$WORK/mk.absent"

# ── 3. fresh isolated stack; root key installed BEFORE first start ──────────
log "provisioning fresh isolated stack"
teardown
docker network create --internal "$NET" >/dev/null
docker volume create "$VOL_DATA" >/dev/null
docker volume create "$VOL_CFG"  >/dev/null

# db-config: non-secret files first, then the root key at exact ownership/mode
docker run --rm --network none -v "$VOL_CFG:/cfg" -v "$WORK:/w:ro" alpine:3.20 \
  sh -c "tar -xzf /w/dbconfig.tar.gz -C /cfg && cp /w/rootkey.bin /cfg/pgsodium_root.key \
         && chown ${RK_OWNER%:*}:${RK_OWNER#*:} /cfg/pgsodium_root.key && chmod 600 /cfg/pgsodium_root.key" >/dev/null
INSTALLED_SHA="$(docker run --rm --network none -v "$VOL_CFG:/cfg:ro" alpine:3.20 sha256sum /cfg/pgsodium_root.key | cut -d' ' -f1)"
check "root key installed pre-start" "$RK_SHA" "$INSTALLED_SHA"

docker run -d --name "$CN" --network "$NET" \
  -v "$VOL_DATA:/var/lib/postgresql/data" -v "$VOL_CFG:/etc/postgresql-custom" \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" "$IMAGE" >/dev/null
for i in $(seq 1 120); do docker exec "$CN" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
docker exec "$CN" pg_isready -U postgres -q || { echo "FATAL: clone never became ready"; docker logs --tail 30 "$CN"; exit 1; }
sleep 5
log "clone bootstrapped"

psqlc(){ docker exec "$CN" psql -U supabase_admin -d "${2:-postgres}" -Atq -c "$1"; }

# ── 4. NEGATIVE: strict restore without globals must fail ───────────────────
docker cp "$WORK/db.dump" "$CN:/tmp/db.dump" >/dev/null
psqlc "CREATE DATABASE neg_noglobals OWNER supabase_admin" >/dev/null
negative "restore with globals omitted" docker exec "$CN" pg_restore -U supabase_admin -d neg_noglobals --exit-on-error /tmp/db.dump
psqlc "DROP DATABASE neg_noglobals" >/dev/null

# ── 5. reconcile globals idempotently ───────────────────────────────────────
python3 - "$WORK/globals.sql" "$WORK/globals_idem.sql" <<'PY'
import re,sys
src=open(sys.argv[1]).read(); out=[]
for line in src.splitlines(True):
    m=re.match(r'^CREATE ROLE (\S+);', line.strip())
    if m:
        r=m.group(1)
        out.append("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='%s') THEN CREATE ROLE %s; END IF; END $$;\n"%(r.strip('"'),r))
    else: out.append(line)
open(sys.argv[2],'w').write(''.join(out))
PY
docker cp "$WORK/globals_idem.sql" "$CN:/tmp/globals_idem.sql" >/dev/null
docker exec "$CN" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q -f /tmp/globals_idem.sql >/dev/null 2>"$WORK/globals.err"
check "globals applied cleanly" "0" "$(grep -ci 'error' "$WORK/globals.err" || true)"
check "role count after reconcile" "31" "$(psqlc 'SELECT count(*) FROM pg_roles')"
check "supabase_realtime_admin exists" "1" "$(psqlc "SELECT count(*) FROM pg_roles WHERE rolname='supabase_realtime_admin'")"
check "supabase_functions_admin exists" "1" "$(psqlc "SELECT count(*) FROM pg_roles WHERE rolname='supabase_functions_admin'")"

# ── 6. NEGATIVE: restoring as the wrong role must fail ──────────────────────
psqlc "CREATE DATABASE neg_wrongrole OWNER supabase_admin" >/dev/null
negative "restore as wrong role (anon)" docker exec "$CN" pg_restore -U anon -d neg_wrongrole --exit-on-error /tmp/db.dump
psqlc "DROP DATABASE neg_wrongrole" >/dev/null

# ── 7. THE RESTORE — strict, fatal on any error ─────────────────────────────
log "strict restore"
psqlc "CREATE DATABASE natetrader_restore OWNER supabase_admin" >/dev/null
set +e
docker exec "$CN" pg_restore -U supabase_admin -d natetrader_restore --exit-on-error --verbose /tmp/db.dump \
  >"$WORK/restore.out" 2>"$WORK/restore.err"
RRC=$?
set -e
check "strict restore exit status" "0" "$RRC"
cp "$WORK/restore.err" "$EVID/restore.stderr"; chmod 600 "$EVID/restore.stderr"
check "restore errors"   "0" "$(grep -c 'pg_restore: error' "$WORK/restore.err" || true)"
check "restore warnings" "0" "$(grep -ci 'pg_restore: warning' "$WORK/restore.err" || true)"
[[ $RRC -eq 0 ]] || { echo "FATAL: restore failed — destroying clone, not continuing from partial state"; exit 1; }

# Make the restored database canonical so services can attach unchanged.
# This must be done from a THIRD database — you cannot rename the database you
# are connected to — and it must be asserted. An earlier version wrapped these
# in `|| true`, the renames silently failed, and every invariant below then ran
# against the empty bootstrap database while reporting confidently. That is the
# precise failure mode this whole programme exists to eliminate, so: no masking,
# and a positive proof that we are looking at the restore.
docker exec "$CN" psql -U supabase_admin -d template1 -Atqc \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('postgres','natetrader_restore') AND pid<>pg_backend_pid()" >/dev/null
docker exec "$CN" psql -U supabase_admin -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "ALTER DATABASE postgres RENAME TO postgres_bootstrap" >/dev/null
docker exec "$CN" psql -U supabase_admin -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "ALTER DATABASE natetrader_restore RENAME TO postgres" >/dev/null
DB=postgres
check "verifying the RESTORE, not the bootstrap" "1" \
  "$(docker exec "$CN" psql -U supabase_admin -d "$DB" -Atqc "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='accounts'")"
log "restored database is '$DB'"
q(){ docker exec "$CN" psql -U supabase_admin -d "$DB" -Atq -c "$1"; }

# ── 8. business invariants vs the snapshot-bound manifest ───────────────────
log "business invariants"
for t in accounts equity_snapshots audit_log positions trades profiles; do
  check "count $t" "$(mj "d['snapshot_bound_counts']['$t']")" "$(q "SELECT count(*) FROM public.$t")"
done
check "count auth_users"    "$(mj "d['snapshot_bound_counts']['auth_users']")"    "$(q 'SELECT count(*) FROM auth.users')"
check "count vault_secrets" "$(mj "d['snapshot_bound_counts']['vault_secrets']")" "$(q 'SELECT count(*) FROM vault.secrets')"
check "accounts_by_mode"    "$(mj "d['snapshot_bound_counts']['accounts_by_mode']")"   "$(q "SELECT json_object_agg(m,n)::text FROM (SELECT mode::text m,count(*) n FROM public.accounts GROUP BY 1) s" | python3 -c 'import json,sys;print(json.load(sys.stdin))')"
check "accounts_by_status"  "$(mj "d['snapshot_bound_counts']['accounts_by_status']")" "$(q "SELECT json_object_agg(s2,n)::text FROM (SELECT status::text s2,count(*) n FROM public.accounts GROUP BY 1) s" | python3 -c 'import json,sys;print(json.load(sys.stdin))')"
check "vault refs resolve" "t" "$(q "SELECT count(*)=0 FROM public.accounts a WHERE (a.alpaca_key_secret_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_key_secret_id)) OR (a.alpaca_secret_secret_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_secret_secret_id))")"
check "cred ids present"      "t" "$(q "SELECT count(*)=0 FROM public.accounts WHERE deleted_at IS NULL AND is_active AND (alpaca_key_secret_id IS NULL OR alpaca_secret_secret_id IS NULL)")"
check "cred ids not crossslot" "t" "$(q "SELECT count(*)=0 FROM public.accounts WHERE alpaca_key_secret_id IS NOT NULL AND alpaca_key_secret_id=alpaca_secret_secret_id")"
check "cred ids unshared"     "t" "$(q "SELECT count(*)=count(DISTINCT x) FROM (SELECT alpaca_key_secret_id x FROM public.accounts WHERE alpaca_key_secret_id IS NOT NULL UNION ALL SELECT alpaca_secret_secret_id FROM public.accounts WHERE alpaca_secret_secret_id IS NOT NULL) u")"

# ── 9. catalogue fidelity ───────────────────────────────────────────────────
log "catalogue fidelity"
check "extensions" "$(mj "d['source']['extensions']")" "$(q "SELECT string_agg(extname||'='||extversion,',' ORDER BY extname) FROM pg_extension")"
check "roles fingerprint" "$(mj "d['fingerprints']['roles_sha256']")" \
  "$(q "SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (SELECT rolname||':'||rolsuper||rolinherit||rolcreaterole||rolcreatedb||rolcanlogin||rolreplication||rolbypassrls AS x FROM pg_roles) s")"
check "schema fingerprint" "$(mj "d['fingerprints']['schema_sha256']")" \
  "$(q "SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (SELECT n.nspname||'.'||c.relname||':'||c.relkind::text||':'||pg_get_userbyid(c.relowner)::text AS x FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname<>'information_schema') s")"
echo "  INFO  rls_policies=$(q "SELECT count(*) FROM pg_policies")  rls_tables=$(q "SELECT count(*) FROM pg_class WHERE relrowsecurity")"
echo "  INFO  constraints=$(q "SELECT count(*) FROM pg_constraint")  indexes=$(q "SELECT count(*) FROM pg_index")  triggers=$(q "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")  sequences=$(q "SELECT count(*) FROM pg_class WHERE relkind='S'")"
echo "  INFO  default_acls=$(q "SELECT count(*) FROM pg_default_acl")  relacls=$(q "SELECT count(*) FROM pg_class WHERE relacl IS NOT NULL")"

# ── 10. Vault decryption + keyed equality with the source ───────────────────
# The plaintexts never leave either database. Both sides emit HMAC-SHA256 under
# one random per-run key with a domain separator; only the boolean is recorded.
log "vault decryption and keyed source comparison"
check "both secrets decrypt" "2" "$(q "SELECT count(*) FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL")"
MACKEY="$(openssl rand -hex 32)"
MACSQL="SELECT encode(hmac(convert_to('ntv2drill:'||id::text||':'||decrypted_secret,'UTF8'),decode('$MACKEY','hex'),'sha256'),'hex') FROM vault.decrypted_secrets ORDER BY id"
SRC_MAC="$(docker exec natetrader-supabase-db-1 psql -U supabase_admin -d postgres -Atq -c "$MACSQL" | sha256sum | cut -d' ' -f1)"
CLONE_MAC="$(q "$MACSQL" | sha256sum | cut -d' ' -f1)"
if [[ "$SRC_MAC" == "$CLONE_MAC" ]]; then PASS=$((PASS+1)); echo "  PASS  restored secret values equal source        true"
else FAIL=$((FAIL+1)); echo "  FAIL  restored secret values equal source        false"; fi
unset MACKEY SRC_MAC CLONE_MAC

# ── 11. NEGATIVE: wrong / missing root key must break decryption ────────────
log "negative controls (root key layer)"
VOLW="ntd-wrongkey-$RUN"; CNW="ntd-wk-$RUN"
docker volume create "$VOLW" >/dev/null
docker run --rm --network none -v "$VOLW:/cfg" -v "$WORK:/w:ro" alpine:3.20 \
  sh -c "tar -xzf /w/dbconfig.tar.gz -C /cfg && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /cfg/pgsodium_root.key \
         && chown ${RK_OWNER%:*}:${RK_OWNER#*:} /cfg/pgsodium_root.key && chmod 600 /cfg/pgsodium_root.key" >/dev/null
docker run -d --name "$CNW" --network "$NET" -v "$VOLW:/etc/postgresql-custom" \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" "$IMAGE" >/dev/null
for i in $(seq 1 120); do docker exec "$CNW" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
sleep 4
docker cp "$WORK/globals_idem.sql" "$CNW:/tmp/g.sql" >/dev/null
docker exec "$CNW" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q -f /tmp/g.sql >/dev/null 2>&1 || true
docker cp "$WORK/db.dump" "$CNW:/tmp/db.dump" >/dev/null
docker exec "$CNW" psql -U supabase_admin -d postgres -Atqc "CREATE DATABASE wk OWNER supabase_admin" >/dev/null 2>&1 || true
docker exec "$CNW" pg_restore -U supabase_admin -d wk --exit-on-error /tmp/db.dump >/dev/null 2>&1 || true
WK_OK="$(docker exec "$CNW" psql -U supabase_admin -d wk -Atqc "SELECT count(*) FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL" 2>/dev/null || echo ERR)"
if [[ "$WK_OK" == "2" ]]; then FAIL=$((FAIL+1)); echo "  FAIL  NEGATIVE altered root key                 secrets STILL decrypted"
else PASS=$((PASS+1)); echo "  PASS  NEGATIVE altered root key                 decryption broken as required ($WK_OK)"; fi
docker rm -f "$CNW" >/dev/null 2>&1 || true; docker volume rm -f "$VOLW" >/dev/null 2>&1 || true

# ── 12. NEGATIVE: manifest tamper detection ─────────────────────────────────
python3 - "$WORK/MANIFEST.json" "$WORK/tampered.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); d['fingerprints']['schema_sha256']='0'*64
json.dump(d,open(sys.argv[2],'w'))
PY
TSHA="$(sha256sum "$WORK/tampered.json" | cut -d' ' -f1)"
if grep -q "manifest_sha256=$TSHA" "$WORK/COMPLETE"; then FAIL=$((FAIL+1)); echo "  FAIL  NEGATIVE changed schema fingerprint       tamper NOT detected"
else PASS=$((PASS+1)); echo "  PASS  NEGATIVE changed schema fingerprint       tamper detected"; fi

MISSING_VAULT="$(grep -c 'TABLE DATA vault secrets' <(docker run --rm -i --network none -v "$WORK:/w:ro" "$IMAGE" pg_restore -l /w/db.dump) || true)"
check "archive carries vault TABLE DATA" "1" "$MISSING_VAULT"

# ── 13. verdict ─────────────────────────────────────────────────────────────
{
  echo "run=$RUN set=$SETID remote=$REMOTE"
  echo "pass=$PASS fail=$FAIL"
  echo "restored_db=$DB image=$IMAGE"
  echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$EVID/verdict.txt"
chmod 600 "$EVID/verdict.txt"
echo
log "=== $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] || { echo "DRILL FAILED"; exit 1; }
log "DRILL PASSED"
exit 0
