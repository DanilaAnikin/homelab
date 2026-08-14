#!/usr/bin/env bash
# ============================================================================
# nt-restore-drill.sh — prove a V2 recovery set actually restores.
#
# Usage: nt-restore-drill.sh <set_id> [remote_index]
#          remote_index 0 = r2 (primary, default), 1 = r2dr (DR copy)
#
# WHAT CHANGED, AND WHY (audit findings C-1..C-4, H-1..H-7)
# ---------------------------------------------------------
# The previous version of this file printed "44 passed / 0 failed". The count
# was real; it measured almost nothing. Specifically:
#
#   * check() compared two strings. Both came from command substitution, and a
#     failing command substitution does not trip `set -e` in an argument list,
#     so a dead container, a SQL error, a missing manifest key and a SQL NULL
#     all produced "" — and `check "" ""` scored PASS. Eleven sites.
#   * the schema "fingerprint" hashed nspname.relname:relkind:owner. Dropping
#     EVERY RLS policy left it byte-identical. Measured: the legacy fingerprint
#     was blind to 17 of 18 one-variable security mutations.
#   * RLS, ACLs, default privileges, constraints, indexes, triggers and
#     sequences were printed as INFO and compared to nothing.
#   * two negative controls could not fail: one asserted the sha256 of a file
#     it had just written differed from a recorded digest (true by
#     construction), the other never substituted the tampered manifest at all.
#   * the "altered root key" control passed for at least six wrong reasons and
#     printed the wrong reason inside its own PASS message.
#   * decryption discarded gpg's exit status and handed the plaintext to a
#     consumer before authentication. Measured: 600477 bytes released on every
#     corruption class.
#   * two failure channels existed — counted FAILs, which wrote a verdict, and
#     silent `set -e` aborts, which wrote nothing at all and produced no
#     "DRILL FAILED" string for a wrapper to key on.
#
# Every comparison here is a typed record via ntv_check. Every decryption goes
# through the quarantine primitive. The catalogue is the byte-identical file
# the builder used. Every exit path lands in exactly one authenticated verdict.
# Assertion counts are diagnostic; the verdict is not derived from them alone.
#
# Secret handling: the pgsodium root key and Vault plaintexts are never
# printed, never written outside tmpfs, and never leave the database except as
# a keyed digest under a fresh per-run key. Only booleans reach evidence.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true   # ERR inheritance into subshells
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/nt-verify.sh
. "$HERE/lib/nt-verify.sh"
# shellcheck source=lib/nt-crypto.sh
. "$HERE/lib/nt-crypto.sh"
CATSQL="$HERE/lib/nt-catalogue.sql"

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
VOLW="ntd-wrongkey-$RUN"; CNW="ntd-wk-$RUN"
EVID="/home/anakin/nt-r0/${SETID}-r${RIDX}"
WORK=""
PW_SALT="$(openssl rand -hex 32)"     # fresh per run; never recorded

log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }

# ── negative controls with an asserted failure class ────────────────────────
# The old negative() accepted ANY non-zero status. That is how "restore as the
# wrong role" passed by failing at connection setup, and how a control with a
# typo in the command scored identically to a real rejection. Every control
# here names the class it expects.
ntv_negative(){ # ntv_negative <name> <expected_class> <function> [args...]
  local name="$1" want="$2"; shift 2
  NTC_ERR=''; NTV_NEG_CLASS=''
  if "$@" >/dev/null 2>&1; then
    NTV_FAIL=$((NTV_FAIL+1))
    [[ -n "$NTV_FIRST_FAILURE" ]] || NTV_FIRST_FAILURE="[$NTV_STAGE] NEGATIVE $name: unexpectedly succeeded"
    printf '  FAIL  NEGATIVE %-40s unexpectedly SUCCEEDED\n' "$name"
    return 0
  fi
  local got="${NTV_NEG_CLASS:-${NTC_ERR:-unclassified}}"
  if [[ -n "$want" && "$want" != any && "$got" != "$want" ]]; then
    NTV_FAIL=$((NTV_FAIL+1))
    [[ -n "$NTV_FIRST_FAILURE" ]] || NTV_FIRST_FAILURE="[$NTV_STAGE] NEGATIVE $name: wrong reason ($got, wanted $want)"
    printf '  FAIL  NEGATIVE %-40s rejected for the WRONG reason: %s (wanted %s)\n' "$name" "$got" "$want"
  else
    NTV_PASS=$((NTV_PASS+1))
    printf '  PASS  NEGATIVE %-40s rejected: %s\n' "$name" "$got"
  fi
}

# NOTE on self-healing/CLAUDE.md's iron rule "NIKDY docker volume rm":
# that rule protects PRODUCTION data volumes. Everything removed here is
# created by this script moments earlier and is uniquely named per run
# (ntd-data-drill-…, ntd-cfg-drill-…). No production volume, container or
# network can match these names, and this script never touches the
# natetrader-* stack except to read from it.
teardown(){
  local rc=0
  docker rm -f "$CN" "$CNW"                >/dev/null 2>&1 || true
  docker volume rm -f "$VOL_DATA" "$VOL_CFG" "$VOLW" >/dev/null 2>&1 || true
  docker network rm "$NET"                 >/dev/null 2>&1 || true
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    mountpoint -q "$WORK" && { umount "$WORK" || rc=1; }
    rmdir "$WORK" 2>/dev/null || rm -rf "$WORK" 2>/dev/null || rc=1
  fi
  # A cleanup failure means decryptable plaintext may still be mounted. That is
  # a FAIL, not a warning — ntv_write_verdict folds cleanup_rc into the verdict.
  return $rc
}
NTV_CLEANUP_HOOK=teardown

mkdir -p "$EVID"; chmod 700 "$EVID"
ntv_verdict_init "$EVID/verdict.txt"
ntv_install_traps
ntv_stage bootstrap

[[ $EUID -eq 0 ]] || { echo "must run as root"; exit 1; }
WORK="$(mktemp -d /run/nt-drill.XXXXXX)"
mount -t tmpfs -o size=8G,mode=0700,noexec,nosuid,nodev tmpfs "$WORK"
export NTV_TMP="$WORK"
export NTC_QUARANTINE_DIR="$WORK"

log "=== drill $RUN : set $SETID from $REMOTE ==="

# ── 0. completion marker and manifest ───────────────────────────────────────
ntv_stage marker
# `rclone cat` exits 0 and emits nothing for an object that does not exist, so
# exit status alone is not a presence test. This is the ONE production gate the
# old drill exercised honestly, and it stays.
marker_valid(){ # marker_valid <remote_path> <dest>
  local dest="${2:-$WORK/COMPLETE}"
  : > "$dest"
  $RC cat "$1" > "$dest" 2>/dev/null || true
  if [[ ! -s "$dest" ]]; then NTV_NEG_CLASS=marker_absent_or_empty; return 1; fi
  if ! grep -q '^set_id=' "$dest"; then NTV_NEG_CLASS=marker_malformed; return 1; fi
  return 0
}
marker_valid "$REMOTE/$PREFIX/COMPLETE" || { echo "FATAL: no valid COMPLETE marker"; exit 1; }
grep -q "^set_id=$SETID$" "$WORK/COMPLETE" || { echo "FATAL: marker names a different set"; exit 1; }
$RC cat "$REMOTE/$PREFIX/MANIFEST.json" > "$WORK/MANIFEST.json"
MSHA="$(sha256sum "$WORK/MANIFEST.json" | cut -d' ' -f1)"
grep -q "manifest_sha256=$MSHA" "$WORK/COMPLETE" || { echo "FATAL: manifest digest does not match marker"; exit 1; }
log "completion marker and manifest digest agree"

M(){ ntv_json "$WORK/MANIFEST.json" "$1" "$2"; }   # typed manifest accessor
mval(){ local r; r="$(M "$1" "$2")"; ntv_parse "$r"; [[ "$NTV_STATUS" == ok ]] || { echo "FATAL: manifest key unusable ($1): $NTV_STATUS"; exit 1; }; printf '%s' "$NTV_VALUE"; }

IMAGE="$(mval "d['source']['image_ref']" text)"
IMAGE_DIGEST="$(mval "d['source']['image_repo_digest']" text)"
RK_OWNER="$(mval "d['root_key']['owner']" text)"
RK_SHA="$(mval "d['root_key']['sha256']" sha256)"

# The manifest records an immutable digest; booting a MUTABLE tag while
# claiming the digest was verified is one of the audit's medium findings.
if [[ "$IMAGE_DIGEST" != NONE && "$IMAGE_DIGEST" == *@sha256:* ]]; then
  BOOT_IMAGE="$IMAGE_DIGEST"
else
  BOOT_IMAGE="$IMAGE"
fi
log "boot image: $BOOT_IMAGE"

# ── 1. fetch, then decrypt through the authenticated primitive ──────────────
ntv_stage components
for c in db.dump.gpg globals.sql.gpg dbconfig.tar.gz.gpg rootkey.bin.gpg; do
  $RC cat "$REMOTE/$PREFIX/$c" > "$WORK/$c"
  ntv_check "ciphertext digest $c" \
    "$(M "[x['ciphertext_sha256'] for x in d['components'] if x['name']=='$c'][0]" sha256)" \
    "$(ntv_ok sha256 "$(sha256sum "$WORK/$c" | cut -d' ' -f1)")"
done

# Decrypt: gpg writes into a quarantine file, its exit status is captured, and
# the bytes are destroyed unless status, size, digest and packet format all
# bind. No consumer ever sees an unauthenticated pathname.
declare -A COMPKEY=( [db.dump]="$KEY_DB" [globals.sql]="$KEY_DB" [dbconfig.tar.gz]="$KEY_DB" [rootkey.bin]="$KEY_ROOT" )
for p in db.dump globals.sql dbconfig.tar.gz rootkey.bin; do
  want_sha="$(mval "[x['plaintext_sha256'] for x in d['components'] if x['name']=='$p.gpg'][0]" sha256)"
  if ntc_decrypt_validated "$WORK/$p.gpg" "${COMPKEY[$p]}" "$want_sha" '-' any "$SETID"; then
    mv "$NTC_VALIDATED_PATH" "$WORK/$p"; NTC_VALIDATED_PATH=''
    ntv_check "authenticated decrypt $p" "$(ntv_ok text ok)" "$(ntv_ok text ok)"
  else
    ntv_check "authenticated decrypt $p" "$(ntv_ok text ok)" "$(ntv_bad "$NTC_ERR" text)"
    echo "FATAL: $p did not authenticate ($NTC_ERR)"; exit 1
  fi
done

# ── 2. NEGATIVE CONTROLS on the encrypted material ──────────────────────────
ntv_stage neg-crypto
log "negative controls (encryption / marker layer)"
mutate_copy(){ cp "$WORK/db.dump.gpg" "$WORK/corrupt.gpg"
  local S; S=$(stat -c %s "$WORK/corrupt.gpg")
  printf '\xff' | dd of="$WORK/corrupt.gpg" bs=1 seek=$((S-8)) conv=notrunc status=none; }
mutate_copy
DB_SHA="$(mval "[x['plaintext_sha256'] for x in d['components'] if x['name']=='db.dump.gpg'][0]" sha256)"
ntv_negative "corrupt encrypted component"   gpg_status      ntc_decrypt_validated "$WORK/corrupt.gpg"      "$KEY_DB"   "$DB_SHA" '-' any "$SETID"
ntv_negative "wrong key for root component"  gpg_status      ntc_decrypt_validated "$WORK/rootkey.bin.gpg"  "$KEY_DB"   "$RK_SHA" '-' any "$SETID"
ntv_negative "missing completion marker"     marker_absent_or_empty  marker_valid "$REMOTE/$PREFIX/COMPLETE.absent" "$WORK/mk.absent"

# manifest tamper must be caught BY THE REAL GATE, not by an assertion the
# control constructs for itself (audit C-3)
python3 - "$WORK/MANIFEST.json" "$WORK/tampered.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); d['fingerprints']['catalogue_sha256']='0'*64
json.dump(d,open(sys.argv[2],'w'))
PY
real_manifest_gate(){ # the production gate, invoked on a substituted manifest
  local m="$1" s; s="$(sha256sum "$m" | cut -d' ' -f1)"
  if ! grep -q "manifest_sha256=$s" "$WORK/COMPLETE"; then NTV_NEG_CLASS=manifest_digest_mismatch; return 1; fi
  return 0
}
ntv_negative "tampered manifest vs real gate" manifest_digest_mismatch real_manifest_gate "$WORK/tampered.json"
ntv_check "real gate accepts the genuine manifest" "$(ntv_ok text ok)" \
  "$(real_manifest_gate "$WORK/MANIFEST.json" && ntv_ok text ok || ntv_bad gate_rejected_genuine text)"

# ── 2b. archive provenance BEFORE anything is restored (audit C-4) ──────────
ntv_stage archive
ntc_archive_declares "$BOOT_IMAGE" "$WORK/db.dump" 'TABLE DATA vault secrets' \
  && d1="$(ntv_ok text ok)" || d1="$(ntv_bad "$NTC_ERR" text)"
ntv_check "archive declares vault TABLE DATA" "$(ntv_ok text ok)" "$d1"
ntc_archive_fully_consumable "$BOOT_IMAGE" "$WORK/db.dump" \
  && d2="$(ntv_ok text ok)" || d2="$(ntv_bad "$NTC_ERR" text)"
ntv_check "archive is fully consumable end to end" "$(ntv_ok text ok)" "$d2"
head -c $(( $(stat -c %s "$WORK/db.dump") / 2 )) "$WORK/db.dump" > "$WORK/halfdump"
ntv_negative "half-length archive rejected" archive_not_consumable \
  ntc_archive_fully_consumable "$BOOT_IMAGE" "$WORK/halfdump"
rm -f "$WORK/halfdump"

# ── 3. fresh isolated stack; root key installed BEFORE first start ──────────
ntv_stage provision
log "provisioning fresh isolated stack"
teardown || true
docker network create --internal "$NET" >/dev/null
docker volume create "$VOL_DATA" >/dev/null
docker volume create "$VOL_CFG"  >/dev/null

docker run --rm --network none -v "$VOL_CFG:/cfg" -v "$WORK:/w:ro" alpine:3.20 \
  sh -c "tar -xzf /w/dbconfig.tar.gz -C /cfg && cp /w/rootkey.bin /cfg/pgsodium_root.key \
         && chown ${RK_OWNER%:*}:${RK_OWNER#*:} /cfg/pgsodium_root.key && chmod 600 /cfg/pgsodium_root.key" >/dev/null
ntv_check "root key installed pre-start" "$(ntv_ok sha256 "$RK_SHA")" \
  "$(ntv_ok sha256 "$(docker run --rm --network none -v "$VOL_CFG:/cfg:ro" alpine:3.20 sha256sum /cfg/pgsodium_root.key | cut -d' ' -f1)")"

# db-config member allowlist (audit H-5): an absent volume is silently created
# empty by `docker run -v name:/src`, tar over an empty dir succeeds, and the
# set publishes with no postgresql.conf and no pg_hba.conf.
CFG_MEMBERS="$(docker run --rm --network none -v "$VOL_CFG:/cfg:ro" alpine:3.20 sh -c 'ls -A /cfg | sort | tr "\n" ","')"
for want in postgresql.conf pg_hba.conf pgsodium_root.key; do
  ntv_check "db-config carries $want" "$(ntv_ok text yes)" \
    "$(grep -q "\b$want," <<<"$CFG_MEMBERS," && ntv_ok text yes || ntv_bad missing text)"
done

docker run -d --name "$CN" --network "$NET" \
  -v "$VOL_DATA:/var/lib/postgresql/data" -v "$VOL_CFG:/etc/postgresql-custom" \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" "$BOOT_IMAGE" >/dev/null
ntv_wait_ready "$CN" postgres supabase_admin 5 240 \
  || { echo "FATAL: clone never became semantically ready"; docker logs --tail 30 "$CN"; exit 1; }
log "clone bootstrapped (ready after ${NTV_READY_AFTER}s)"

ntv_define_psql "$CN" postgres supabase_admin

# ── 4. NEGATIVE: strict restore without globals must fail ───────────────────
ntv_stage neg-globals
docker cp "$WORK/db.dump" "$CN:/tmp/db.dump" >/dev/null
ntv_sql text0 "CREATE DATABASE neg_noglobals OWNER supabase_admin" >/dev/null
restore_into(){ # <db> <role>
  local rc=0
  docker exec "$CN" pg_restore -U "$2" -d "$1" --exit-on-error /tmp/db.dump >/dev/null 2>"$WORK/neg.err" || rc=$?
  if [[ $rc -ne 0 ]]; then
    if grep -qiE 'role .* does not exist|must be owner|permission denied' "$WORK/neg.err"; then
      NTV_NEG_CLASS=restore_missing_role_or_privilege
    elif grep -qiE 'could not connect|authentication failed|no pg_hba' "$WORK/neg.err"; then
      NTV_NEG_CLASS=connection_refused    # NOT a valid restore-authorization result
    else NTV_NEG_CLASS=restore_error; fi
    return 1
  fi
  return 0
}
ntv_negative "restore with globals omitted" restore_missing_role_or_privilege restore_into neg_noglobals supabase_admin
ntv_sql text0 "DROP DATABASE neg_noglobals" >/dev/null

# ── 5. reconcile globals idempotently ───────────────────────────────────────
ntv_stage globals
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
GRC=0
docker exec "$CN" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q -f /tmp/globals_idem.sql \
  >/dev/null 2>"$WORK/globals.err" || GRC=$?
ntv_check "globals applied cleanly" "$(ntv_ok int 0)" "$(ntv_ok int "$GRC")"
# role count comes from the MANIFEST, not a hardcoded 31 (audit medium finding)
ntv_check "role count after reconcile" \
  "$(M "d['archive']['globals_create_role_count']" int)" "$(ntv_sql int 'SELECT count(*) FROM pg_roles')"
for r in supabase_realtime_admin supabase_functions_admin; do
  ntv_check "role $r exists" "$(ntv_ok int 1)" "$(ntv_sql int "SELECT count(*) FROM pg_roles WHERE rolname='$r'")"
done

# ── 6. NEGATIVE: restoring as an unprivileged role must fail FOR THAT REASON ─
ntv_stage neg-role
# The old control used `anon`, which is NOLOGIN, so it failed at connection and
# never tested restore authorization at all. `authenticated` is also NOLOGIN in
# this image, so we create a real LOGIN role with no privileges — otherwise the
# control passes for the wrong reason and would score identically with a typo.
ntv_sql text0 "CREATE ROLE ntd_unpriv LOGIN" >/dev/null
ntv_sql text0 "CREATE DATABASE neg_wrongrole OWNER supabase_admin" >/dev/null
ntv_negative "restore as an unprivileged LOGIN role" restore_missing_role_or_privilege \
  restore_into neg_wrongrole ntd_unpriv
ntv_sql text0 "DROP DATABASE neg_wrongrole" >/dev/null

# ── 7. THE RESTORE — strict, fatal on any error ─────────────────────────────
ntv_stage restore
log "strict restore"
ntv_sql text0 "CREATE DATABASE natetrader_restore OWNER supabase_admin" >/dev/null
RRC=0
docker exec "$CN" pg_restore -U supabase_admin -d natetrader_restore --exit-on-error --verbose /tmp/db.dump \
  >"$WORK/restore.out" 2>"$WORK/restore.err" || RRC=$?
ntv_check "strict restore exit status" "$(ntv_ok int 0)" "$(ntv_ok int "$RRC")"
cp "$WORK/restore.err" "$EVID/restore.stderr"; chmod 600 "$EVID/restore.stderr"
ntv_check "restore errors"   "$(ntv_ok int 0)" "$(ntv_ok int "$(grep -c 'pg_restore: error'   "$WORK/restore.err" || true)")"
ntv_check "restore warnings" "$(ntv_ok int 0)" "$(ntv_ok int "$(grep -ci 'pg_restore: warning' "$WORK/restore.err" || true)")"
[[ $RRC -eq 0 ]] || { echo "FATAL: restore failed — not continuing from partial state"; exit 1; }

# Make the restored database canonical. An earlier version wrapped these in
# `|| true`, the renames silently failed, and every invariant below ran against
# the empty bootstrap database while reporting confidently.
docker exec "$CN" psql -U supabase_admin -d template1 -Atqc \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('postgres','natetrader_restore') AND pid<>pg_backend_pid()" >/dev/null
docker exec "$CN" psql -U supabase_admin -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "ALTER DATABASE postgres RENAME TO postgres_bootstrap" >/dev/null
docker exec "$CN" psql -U supabase_admin -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "ALTER DATABASE natetrader_restore RENAME TO postgres" >/dev/null
ntv_check "verifying the RESTORE, not the bootstrap" "$(ntv_ok int 1)" \
  "$(ntv_sql int "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='accounts'")"

# ── 8. business invariants vs the snapshot-bound manifest ───────────────────
ntv_stage invariants
log "business invariants"
for t in accounts equity_snapshots audit_log positions trades profiles; do
  ntv_check "count $t" "$(M "d['snapshot_bound_counts']['$t']" int)" "$(ntv_sql int "SELECT count(*) FROM public.$t")"
done
ntv_check "count auth_users"    "$(M "d['snapshot_bound_counts']['auth_users']" int)"    "$(ntv_sql int 'SELECT count(*) FROM auth.users')"
ntv_check "count vault_secrets" "$(M "d['snapshot_bound_counts']['vault_secrets']" int)" "$(ntv_sql int 'SELECT count(*) FROM vault.secrets')"
ntv_check "accounts_by_mode"    "$(M "d['snapshot_bound_counts']['accounts_by_mode']" json)" \
  "$(ntv_sql json "SELECT json_object_agg(m,n)::text FROM (SELECT mode::text m,count(*) n FROM public.accounts GROUP BY 1) s")"
ntv_check "accounts_by_status"  "$(M "d['snapshot_bound_counts']['accounts_by_status']" json)" \
  "$(ntv_sql json "SELECT json_object_agg(s2,n)::text FROM (SELECT status::text s2,count(*) n FROM public.accounts GROUP BY 1) s")"
ntv_check "vault refs resolve" "$(ntv_ok bool_t t)" "$(ntv_sql bool_t "SELECT count(*)=0 FROM public.accounts a WHERE (a.alpaca_key_secret_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_key_secret_id)) OR (a.alpaca_secret_secret_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_secret_secret_id))")"
ntv_check "cred ids present"       "$(ntv_ok bool_t t)" "$(ntv_sql bool_t "SELECT count(*)=0 FROM public.accounts WHERE deleted_at IS NULL AND is_active AND (alpaca_key_secret_id IS NULL OR alpaca_secret_secret_id IS NULL)")"
ntv_check "cred ids not crossslot" "$(ntv_ok bool_t t)" "$(ntv_sql bool_t "SELECT count(*)=0 FROM public.accounts WHERE alpaca_key_secret_id IS NOT NULL AND alpaca_key_secret_id=alpaca_secret_secret_id")"
ntv_check "cred ids unshared"      "$(ntv_ok bool_t t)" "$(ntv_sql bool_t "SELECT count(*)=count(DISTINCT x) FROM (SELECT alpaca_key_secret_id x FROM public.accounts WHERE alpaca_key_secret_id IS NOT NULL UNION ALL SELECT alpaca_secret_secret_id FROM public.accounts WHERE alpaca_secret_secret_id IS NOT NULL) u")"

# ── 9. catalogue fidelity — the canonical catalogue, not a name list ────────
ntv_stage catalogue
log "canonical catalogue"
if ntv_catalogue "$CN" postgres supabase_admin "$PW_SALT" "$CATSQL" "$WORK/catalogue.txt"; then
  ntv_check "catalogue digest" "$(M "d['fingerprints']['catalogue_sha256']" sha256)" "$(ntv_ok sha256 "$NTV_CAT_SHA")"
  ntv_check "catalogue domain census" "$(M "d['fingerprints']['catalogue_census']" json)" "$(ntv_ok json "$NTV_CAT_CENSUS")"
else
  ntv_check "catalogue digest" "$(ntv_ok text ok)" "$(ntv_bad "${NTV_CAT_ERR// /_}" text)"
fi
ntv_check "roles fingerprint" "$(M "d['fingerprints']['roles_sha256']" sha256)" \
  "$(ntv_sql sha256 "SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (SELECT rolname||':'||rolsuper||rolinherit||rolcreaterole||rolcreatedb||rolcanlogin||rolreplication||rolbypassrls AS x FROM pg_roles) s")"
ntv_check "extensions" "$(M "d['source']['extensions']" text)" \
  "$(ntv_sql text "SELECT string_agg(extname||'='||extversion,',' ORDER BY extname) FROM pg_extension")"

# password verifiers: compared against the SET's own globals, under a fresh
# per-run salt, never against live production and never as a stable digest
if ntv_pwverifier_matches_globals "$WORK/catalogue.txt" "$WORK/globals.sql" "$PW_SALT"; then
  ntv_check "password verifiers match the set's globals" "$(ntv_ok text ok)" "$(ntv_ok text ok)"
else
  ntv_check "password verifiers match the set's globals" "$(ntv_ok text ok)" "$(ntv_bad "${NTV_PW_ERR// /_}" text)"
fi

# ── 10. Vault decryption, compared to the SET, not to live production ───────
ntv_stage vault
log "vault decryption"
ntv_check "both secrets decrypt" "$(M "d['snapshot_bound_counts']['vault_secrets']" int)" \
  "$(ntv_sql int "SELECT count(*) FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL")"
# The manifest records a keyed commitment computed at SOURCE time under a
# domain-separated key bound to this set. Comparing to live production at drill
# time — what the old drill did — proves production and the clone agree now,
# not that the SET restores correctly, and it breaks whenever production
# legitimately moves on.
# No `|| true` here. A set whose manifest carries no vault commitment was built
# by the pre-fix builder, and nothing else about it should be trusted either —
# so the absence is a named FAIL, not a silently skipped assertion.
VKEY_REC="$(M "d['vault_commitment']['key_hex']" text)"
ntv_parse "$VKEY_REC"
if [[ "$NTV_STATUS" == ok ]]; then
  VKEY="$NTV_VALUE"
  ntv_check "vault plaintexts match the set commitment" \
    "$(M "d['vault_commitment']['digest']" sha256)" \
    "$(ntv_sql sha256 "SELECT encode(sha256(convert_to(string_agg(encode(hmac(convert_to('ntv2vault:'||id::text||':'||decrypted_secret,'UTF8'),decode('$VKEY','hex'),'sha256'),'hex'),'|' ORDER BY id),'UTF8')),'hex') FROM vault.decrypted_secrets")"
  unset VKEY
else
  ntv_check "manifest carries a vault commitment" "$(ntv_ok text present)" "$(ntv_bad absent text)"
fi

# ── 11. NEGATIVE: wrong root key must break decryption, and ONLY that ───────
ntv_stage neg-rootkey
log "negative control (root key layer)"
# The old control passed for >=6 wrong reasons: no readiness assertion, three
# `|| true`s, and `2>/dev/null || echo ERR` so the string "ERR" scored PASS and
# printed the wrong reason inside the PASS message. Here every unrelated
# prerequisite is asserted FIRST; only then is exactly one variable changed.
docker volume create "$VOLW" >/dev/null
docker run --rm --network none -v "$VOLW:/cfg" -v "$WORK:/w:ro" alpine:3.20 \
  sh -c "tar -xzf /w/dbconfig.tar.gz -C /cfg && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /cfg/pgsodium_root.key \
         && chown ${RK_OWNER%:*}:${RK_OWNER#*:} /cfg/pgsodium_root.key && chmod 600 /cfg/pgsodium_root.key" >/dev/null
docker run -d --name "$CNW" --network "$NET" -v "$VOLW:/etc/postgresql-custom" \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" "$BOOT_IMAGE" >/dev/null
if ! ntv_wait_ready "$CNW" postgres supabase_admin 5 240; then
  ntv_check "wrong-key clone reached readiness (prerequisite)" "$(ntv_ok text ready)" "$(ntv_bad never_ready text)"
else
  ntv_check "wrong-key clone reached readiness (prerequisite)" "$(ntv_ok text ready)" "$(ntv_ok text ready)"
  docker cp "$WORK/globals_idem.sql" "$CNW:/tmp/g.sql" >/dev/null
  WG=0; docker exec "$CNW" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q -f /tmp/g.sql >/dev/null 2>&1 || WG=$?
  ntv_check "wrong-key clone globals applied (prerequisite)" "$(ntv_ok int 0)" "$(ntv_ok int "$WG")"
  docker cp "$WORK/db.dump" "$CNW:/tmp/db.dump" >/dev/null
  docker exec "$CNW" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -Atqc "CREATE DATABASE wk OWNER supabase_admin" >/dev/null
  WR=0; docker exec "$CNW" pg_restore -U supabase_admin -d wk --exit-on-error /tmp/db.dump >/dev/null 2>&1 || WR=$?
  ntv_check "wrong-key clone restored cleanly (prerequisite)" "$(ntv_ok int 0)" "$(ntv_ok int "$WR")"
  ntv_check "wrong-key clone has the vault rows (prerequisite)" \
    "$(M "d['snapshot_bound_counts']['vault_secrets']" int)" \
    "$(ntv_sql_on "$CNW" wk int "SELECT count(*) FROM vault.secrets")"
  # ONLY NOW is the single variable — the root key — the thing under test.
  DECN="$(ntv_sql_on "$CNW" wk int "SELECT count(*) FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL")"
  ntv_parse "$DECN"
  if [[ "$NTV_STATUS" == ok && "$NTV_VALUE" -gt 0 ]]; then
    NTV_FAIL=$((NTV_FAIL+1)); printf '  FAIL  NEGATIVE %-40s secrets STILL decrypted (%s)\n' "altered root key" "$NTV_VALUE"
    [[ -n "$NTV_FIRST_FAILURE" ]] || NTV_FIRST_FAILURE="[$NTV_STAGE] altered root key did not break decryption"
  else
    NTV_PASS=$((NTV_PASS+1)); printf '  PASS  NEGATIVE %-40s decryption broken as required (%s)\n' "altered root key" "$NTV_STATUS"
  fi
fi
docker rm -f "$CNW" >/dev/null 2>&1 || true; docker volume rm -f "$VOLW" >/dev/null 2>&1 || true

# ── 12. verdict ─────────────────────────────────────────────────────────────
ntv_stage verdict
{
  echo "run=$RUN set=$SETID remote=$REMOTE"
  echo "restored_db=postgres boot_image=$BOOT_IMAGE"
  echo "catalogue_lines=${NTV_CAT_LINES:-0}"
} > "$EVID/context.txt"
chmod 600 "$EVID/context.txt"
echo
log "=== $NTV_PASS passed, $NTV_FAIL failed ==="
# The verdict is written by the EXIT trap in every case — normal exit, function
# failure, pipeline failure, subshell failure, signal and cleanup failure — so
# there is exactly one, and a silent abort can no longer report as nothing.
[[ $NTV_FAIL -eq 0 ]] || { echo "DRILL FAILED"; exit 1; }
log "DRILL PASSED"
exit 0
