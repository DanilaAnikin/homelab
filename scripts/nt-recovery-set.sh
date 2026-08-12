#!/usr/bin/env bash
# ============================================================================
# nt-recovery-set.sh — V2 ATOMIC RECOVERY SET for the Nate Trader Supabase stack.
#
# Why this exists (R0 post-mortem, 2026-08-12):
#   The legacy backup path produced a *database archive* that was complete, but
#   an *incomplete recovery set*. A strict restore of that archive onto a fresh
#   supabase/postgres image aborts, because:
#     - the fresh image bootstraps 29 roles, production has 31;
#     - supabase_functions_admin and supabase_realtime_admin are missing;
#     - TOC entry 342 (FUNCTION realtime.topic()) is owned by the missing
#       supabase_realtime_admin, so --exit-on-error stops there and everything
#       after it — including public.accounts and vault.secrets — never loads;
#     - vault.secrets rows are encrypted under the cluster pgsodium root key,
#       which was never captured at all, so even a complete data restore would
#       yield undecryptable secrets.
#   A non-strict restore hid all of this behind ~40 tolerated errors and
#   produced a plausible but hollow clone. Table counts alone went green.
#
# What a V2 set contains (all components cryptographically bound by MANIFEST):
#   1. db.dump.gpg      — pg_dump -Fc of the production database
#   2. globals.sql.gpg  — pg_dumpall --globals-only (roles, attrs, memberships)
#   3. rootkey.gpg      — the pgsodium root key (SEPARATE encryption key)
#   4. dbconfig.tar.gpg — non-secret db-config files needed to boot the image
#   5. MANIFEST.json    — versions, digests, sizes, fingerprints, restore order
#   6. COMPLETE         — completion marker, published LAST, never partial
#
# Design rules enforced here:
#   - authenticated encryption only (GPG AES-256 OCB AEAD); never raw AES-CBC
#   - key separation: the root key uses a different passphrase file than the DB
#   - row counts are bound to the archive's exact MVCC snapshot, not sampled
#     near it (see export_snapshot below)
#   - components upload under .part names, are verified remotely, then renamed;
#     COMPLETE is written only after every component verifies on BOTH remotes
#   - last-success state is never advanced for a partial or failed set
#   - plaintext lives only in tmpfs. We do NOT claim shred() erases anything on
#     SSD/overlay/CoW; we rely on tmpfs never being written to stable storage.
#
# Exit: 0 = complete verified set published. Non-zero = nothing published.
# ============================================================================
set -euo pipefail
umask 077

VERSION=2
CONTAINER="${NT_DB_CONTAINER:-natetrader-supabase-db-1}"
DBNAME="${NT_DB_NAME:-postgres}"
DBUSER="${NT_DB_USER:-supabase_admin}"
DBCONFIG_VOLUME="${NT_DBCONFIG_VOLUME:-natetrader-supabase_db-config}"
ROOTKEY_PATH="/etc/postgresql-custom/pgsodium_root.key"

SECRETS=/srv/homelab/secrets
KEY_DB="$SECRETS/nt-v2-db-key.txt"           # component key: db/globals/config
KEY_ROOT="$SECRETS/nt-v2-rootkey-key.txt"    # component key: pgsodium root key
R2_CONF="$SECRETS/rclone.conf"
REMOTES=("r2:homelab-backups" "r2dr:homelab-backups-dr")

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SETID="nt-v2-$TS"
PREFIX="natetrader-recovery-v2/$SETID"
STATE_DIR=/var/lib/homelab
STATE="$STATE_DIR/nt-recovery-set-last-ok"
LOCK="$STATE_DIR/nt-recovery-set.lock"
KUMA_FILE="$SECRETS/kuma-nt-recovery-push-url.txt"

RC="rclone --config $R2_CONF --retries 8 --retries-sleep 20s --low-level-retries 10 --s3-upload-cutoff 5G"

WORK=""
log(){ echo "[$(date -u +%H:%M:%SZ)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

# Signal-safe cleanup: tmpfs is unmounted and removed on ANY exit path.
cleanup(){
  local rc=$?
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    mountpoint -q "$WORK" && umount "$WORK" 2>/dev/null || true
    rmdir "$WORK" 2>/dev/null || rm -rf "$WORK" 2>/dev/null || true
  fi
  exit $rc
}
trap cleanup EXIT INT TERM HUP

# ── preconditions ───────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "must run as root (needs docker + $SECRETS)"
mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
flock -n 9 || die "another nt-recovery-set run holds the lock"

for f in "$KEY_DB" "$KEY_ROOT" "$R2_CONF"; do [[ -s "$f" ]] || die "missing key/config: $f"; done
[[ "$(stat -c %a "$KEY_DB")" == "600" ]]   || die "$KEY_DB must be mode 0600"
[[ "$(stat -c %a "$KEY_ROOT")" == "600" ]] || die "$KEY_ROOT must be mode 0600"
cmp -s "$KEY_DB" "$KEY_ROOT" && die "key separation violated: db and rootkey passphrases are identical"
command -v gpg >/dev/null || die "gpg not installed"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "container $CONTAINER not running"

WORK="$(mktemp -d /run/nt-recovery.XXXXXX)"
mount -t tmpfs -o size=8G,mode=0700,noexec,nosuid,nodev tmpfs "$WORK" \
  || die "could not mount tmpfs at $WORK (plaintext must never touch disk)"

encrypt(){ # encrypt <plaintext> <out.gpg> <passfile>
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$3" \
      --symmetric --cipher-algo AES256 --force-ocb --s2k-mode 3 \
      --s2k-digest-algo SHA512 --s2k-count 65011712 \
      -o "$2" "$1"
}
verify_decrypts(){ # verify_decrypts <file.gpg> <passfile> <expected_sha256>
  local got
  got="$(gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$2" \
         -d "$1" 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [[ "$got" == "$3" ]]
}

# ── 1+2+3. archive and row counts under ONE held MVCC snapshot ──────────────
# The dump and the counts must describe the same instant, otherwise the
# manifest documents a moment *near* the archive rather than the archive.
# A detached `psql -c "BEGIN; SELECT pg_export_snapshot(); SELECT pg_sleep()"`
# cannot work: -c returns all output only when the whole string finishes, so
# the snapshot id never arrives in time. Instead one psql session holds the
# repeatable-read transaction open and shells out to pg_dump from inside it
# via \! , so pg_dump attaches to the very snapshot the counts are read from.
log "snapshot-bound pg_dump + counts"
cat > "$WORK/capture.sql" <<SQL
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot() AS snap \\gset
\\setenv SNAP :snap
\\! pg_dump -U $DBUSER -Fc -Z6 --snapshot="\$SNAP" $DBNAME > /tmp/nt_db.dump 2>/tmp/nt_db.err; echo "PGDUMP_RC=\$?"
SELECT 'COUNTS='||json_build_object(
  'accounts',        (SELECT count(*) FROM public.accounts),
  'equity_snapshots',(SELECT count(*) FROM public.equity_snapshots),
  'audit_log',       (SELECT count(*) FROM public.audit_log),
  'auth_users',      (SELECT count(*) FROM auth.users),
  'vault_secrets',   (SELECT count(*) FROM vault.secrets),
  'positions',       (SELECT count(*) FROM public.positions),
  'trades',          (SELECT count(*) FROM public.trades),
  'profiles',        (SELECT count(*) FROM public.profiles),
  -- recorded as distributions rather than asserted against guessed enum
  -- labels; the rehearsal compares restored-vs-source exactly either way
  'accounts_by_mode',   (SELECT json_object_agg(m,n) FROM (SELECT mode::text m, count(*) n FROM public.accounts GROUP BY 1) s),
  'accounts_by_status', (SELECT json_object_agg(st,n) FROM (SELECT status::text st, count(*) n FROM public.accounts GROUP BY 1) s),
  -- every non-null credential reference must resolve to a live Vault row
  'vault_refs_ok', (SELECT count(*)=0 FROM public.accounts a WHERE
       (a.alpaca_key_secret_id    IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_key_secret_id))
    OR (a.alpaca_secret_secret_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vault.secrets v WHERE v.id=a.alpaca_secret_secret_id))),
  -- an active account must carry both credential slots
  'cred_ids_present', (SELECT count(*)=0 FROM public.accounts
     WHERE deleted_at IS NULL AND is_active
       AND (alpaca_key_secret_id IS NULL OR alpaca_secret_secret_id IS NULL)),
  -- key and secret must never be the same Vault row (cross-slot reuse)
  'cred_ids_not_crossslot', (SELECT count(*)=0 FROM public.accounts
     WHERE alpaca_key_secret_id IS NOT NULL AND alpaca_key_secret_id = alpaca_secret_secret_id),
  -- no Vault row may be shared between accounts or slots
  'cred_ids_unshared', (SELECT count(*)=count(DISTINCT x) FROM (
        SELECT alpaca_key_secret_id AS x FROM public.accounts WHERE alpaca_key_secret_id IS NOT NULL
        UNION ALL
        SELECT alpaca_secret_secret_id FROM public.accounts WHERE alpaca_secret_secret_id IS NOT NULL) u)
)::text;
COMMIT;
SQL
docker cp "$WORK/capture.sql" "$CONTAINER:/tmp/nt_capture.sql" >/dev/null
CAPOUT="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DBNAME" -Atq -f /tmp/nt_capture.sql 2>&1 || true)"
container_cleanup(){ docker exec "$CONTAINER" rm -f /tmp/nt_capture.sql /tmp/nt_db.dump /tmp/nt_db.err >/dev/null 2>&1 || true; }

grep -q 'PGDUMP_RC=0' <<<"$CAPOUT" || { container_cleanup; die "snapshot-bound pg_dump failed: $(grep -v COUNTS= <<<"$CAPOUT" | head -5)"; }
COUNTS_JSON="$(grep '^COUNTS=' <<<"$CAPOUT" | head -1 | sed 's/^COUNTS=//')"
[[ -n "$COUNTS_JSON" ]] || { container_cleanup; die "snapshot-bound count query produced nothing"; }
python3 -c "import json,sys;json.loads(sys.argv[1])" "$COUNTS_JSON" || { container_cleanup; die "counts are not valid JSON"; }

docker cp "$CONTAINER:/tmp/nt_db.dump" "$WORK/db.dump" >/dev/null || { container_cleanup; die "could not retrieve archive"; }
container_cleanup
rm -f "$WORK/capture.sql"
[[ -s "$WORK/db.dump" ]] || die "pg_dump produced an empty archive"

# The archive must contain the Vault data. supabase_vault >=0.3.1 registers
# vault.secrets via pg_extension_config_dump, so TABLE DATA vault secrets is
# expected. If a future extension version stops doing that, fail loudly rather
# than silently shipping a set whose secrets cannot be recovered.
docker run --rm -i --network none -v "$WORK:/w:ro" \
  "$(docker inspect "$CONTAINER" --format '{{.Config.Image}}')" \
  pg_restore -l /w/db.dump > "$WORK/toc.txt" || die "pg_restore -l failed"
grep -q 'TABLE DATA vault secrets' "$WORK/toc.txt" \
  || die "archive has no 'TABLE DATA vault secrets' entry — refusing to publish"
TOC_ENTRIES="$(grep -c '^[0-9]' "$WORK/toc.txt" || true)"

# ── 4. catalogue fingerprints (non-secret) ──────────────────────────────────
FP_ROLES="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DBNAME" -Atq -c "
SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (
 SELECT rolname||':'||rolsuper||rolinherit||rolcreaterole||rolcreatedb||rolcanlogin||rolreplication||rolbypassrls AS x
 FROM pg_roles) s")"
FP_SCHEMA="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DBNAME" -Atq -c "
SELECT encode(sha256(convert_to(string_agg(x,'|' ORDER BY x),'UTF8')),'hex') FROM (
 SELECT n.nspname||'.'||c.relname||':'||c.relkind::text||':'||pg_get_userbyid(c.relowner)::text AS x
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema') s")"
EXTENSIONS="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DBNAME" -Atq -c \
  "SELECT string_agg(extname||'='||extversion,',' ORDER BY extname) FROM pg_extension")"
PGVER="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DBNAME" -Atq -c 'SHOW server_version')"
IMAGE_REF="$(docker inspect "$CONTAINER" --format '{{.Config.Image}}')"
IMAGE_ID="$(docker inspect "$CONTAINER" --format '{{.Image}}')"
IMAGE_DIGEST="$(docker image inspect "$IMAGE_REF" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}NONE{{end}}' 2>/dev/null || echo NONE)"

# ── 5. globals ──────────────────────────────────────────────────────────────
log "pg_dumpall --globals-only"
docker exec "$CONTAINER" pg_dumpall -U "$DBUSER" --globals-only > "$WORK/globals.sql" || die "globals capture failed"
grep -q '^CREATE ROLE supabase_realtime_admin' "$WORK/globals.sql" \
  || die "globals lack supabase_realtime_admin — the exact role whose absence broke R0"
grep -q '^CREATE ROLE supabase_functions_admin' "$WORK/globals.sql" \
  || die "globals lack supabase_functions_admin"
GLOBALS_ROLES="$(grep -c '^CREATE ROLE' "$WORK/globals.sql")"

# ── 6. pgsodium root key (separate key, never printed) ──────────────────────
log "capturing pgsodium root key"
docker exec "$CONTAINER" test -f "$ROOTKEY_PATH" || die "root key missing at $ROOTKEY_PATH"
# NB: written as an if-block on purpose. `test -L … && die` is a standalone
# compound whose non-zero status (the healthy, non-symlink case) would trip
# `set -e` and abort every good run.
if docker exec "$CONTAINER" test -L "$ROOTKEY_PATH"; then die "root key is a symlink — refusing"; fi
RK_BEFORE="$(docker exec "$CONTAINER" sha256sum "$ROOTKEY_PATH" | cut -d' ' -f1)"
RK_MODE="$(docker exec "$CONTAINER" stat -c %a "$ROOTKEY_PATH")"
RK_OWNER="$(docker exec "$CONTAINER" stat -c %u:%g "$ROOTKEY_PATH")"
RK_SIZE="$(docker exec "$CONTAINER" stat -c %s "$ROOTKEY_PATH")"
[[ "$RK_MODE" == "600" ]] || die "root key mode is $RK_MODE, expected 600"
docker exec "$CONTAINER" grep -qE '^[0-9a-f]{64}$' "$ROOTKEY_PATH" || die "root key is not 64 lowercase hex chars"
docker exec "$CONTAINER" cat "$ROOTKEY_PATH" > "$WORK/rootkey.bin"
[[ "$(sha256sum "$WORK/rootkey.bin" | cut -d' ' -f1)" == "$RK_BEFORE" ]] || die "root key changed during capture"
RK_AFTER="$(docker exec "$CONTAINER" sha256sum "$ROOTKEY_PATH" | cut -d' ' -f1)"
[[ "$RK_AFTER" == "$RK_BEFORE" ]] || die "root key digest unstable across capture"

# ── 7. non-secret db-config (everything in the volume EXCEPT the root key) ──
log "capturing non-secret db-config"
docker run --rm --network none -v "$DBCONFIG_VOLUME:/src:ro" -v "$WORK:/out" alpine:3.20 \
  tar -czf /out/dbconfig.tar.gz -C /src --exclude=pgsodium_root.key . \
  || die "db-config capture failed"

# ── 8. encrypt every component (AEAD), then prove each decrypts ─────────────
declare -A PLAIN=( [db.dump]="$KEY_DB" [globals.sql]="$KEY_DB" [dbconfig.tar.gz]="$KEY_DB" [rootkey.bin]="$KEY_ROOT" )
declare -A SHA_PLAIN SHA_ENC SIZE_ENC
for f in "${!PLAIN[@]}"; do
  key="${PLAIN[$f]}"
  SHA_PLAIN[$f]="$(sha256sum "$WORK/$f" | cut -d' ' -f1)"
  encrypt "$WORK/$f" "$WORK/$f.gpg" "$key" || die "encryption failed for $f"
  verify_decrypts "$WORK/$f.gpg" "$key" "${SHA_PLAIN[$f]}" || die "decrypt verification failed for $f"
  SHA_ENC[$f]="$(sha256sum "$WORK/$f.gpg" | cut -d' ' -f1)"
  SIZE_ENC[$f]="$(stat -c %s "$WORK/$f.gpg")"
  shred -u "$WORK/$f" 2>/dev/null || rm -f "$WORK/$f"   # tmpfs: removal is the guarantee, not shred
done
log "all components encrypted and decrypt-verified"

# ── 9. manifest binds every component to the source state ───────────────────
cat > "$WORK/MANIFEST.json" <<JSON
{
  "recovery_set_version": $VERSION,
  "set_id": "$SETID",
  "created_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": {
    "container": "$CONTAINER",
    "database": "$DBNAME",
    "dump_role": "$DBUSER",
    "postgres_version": "$PGVER",
    "image_ref": "$IMAGE_REF",
    "image_id": "$IMAGE_ID",
    "image_repo_digest": "$IMAGE_DIGEST",
    "extensions": "$EXTENSIONS"
  },
  "fingerprints": { "roles_sha256": "$FP_ROLES", "schema_sha256": "$FP_SCHEMA" },
  "archive": { "toc_entries": $TOC_ENTRIES, "vault_table_data_present": true, "globals_create_role_count": $GLOBALS_ROLES },
  "snapshot_bound_counts": $COUNTS_JSON,
  "root_key": { "path": "$ROOTKEY_PATH", "mode": "$RK_MODE", "owner": "$RK_OWNER", "size": $RK_SIZE, "sha256": "$RK_BEFORE" },
  "encryption": { "format": "gpg-symmetric-aes256-ocb-aead", "key_separation": true,
                  "db_key_file": "$KEY_DB", "rootkey_key_file": "$KEY_ROOT" },
  "components": [
    {"name":"db.dump.gpg","plaintext_sha256":"${SHA_PLAIN[db.dump]}","ciphertext_sha256":"${SHA_ENC[db.dump]}","ciphertext_size":${SIZE_ENC[db.dump]},"key":"db"},
    {"name":"globals.sql.gpg","plaintext_sha256":"${SHA_PLAIN[globals.sql]}","ciphertext_sha256":"${SHA_ENC[globals.sql]}","ciphertext_size":${SIZE_ENC[globals.sql]},"key":"db"},
    {"name":"dbconfig.tar.gz.gpg","plaintext_sha256":"${SHA_PLAIN[dbconfig.tar.gz]}","ciphertext_sha256":"${SHA_ENC[dbconfig.tar.gz]}","ciphertext_size":${SIZE_ENC[dbconfig.tar.gz]},"key":"db"},
    {"name":"rootkey.bin.gpg","plaintext_sha256":"${SHA_PLAIN[rootkey.bin]}","ciphertext_sha256":"${SHA_ENC[rootkey.bin]}","ciphertext_size":${SIZE_ENC[rootkey.bin]},"key":"rootkey"}
  ],
  "restore_order": [
    "1. create fresh data volume and fresh db-config volume",
    "2. decrypt rootkey.bin.gpg into the db-config volume as pgsodium_root.key, uid:gid $RK_OWNER, mode 0600, BEFORE first postgres start",
    "3. decrypt dbconfig.tar.gz.gpg into the same db-config volume (does not overwrite the root key)",
    "4. start image $IMAGE_REF on a private network with no published port; wait for platform bootstrap",
    "5. apply globals.sql with CREATE ROLE guarded idempotently; ON_ERROR_STOP=1",
    "6. createdb target owned by $DBUSER (do NOT restore into the bootstrapped postgres db: platform schemas collide)",
    "7. pg_restore -U $DBUSER -d <target> --exit-on-error --verbose",
    "8. rename databases so the restored one is named $DBNAME if services must attach",
    "9. verify counts, fingerprints, vault decryption, Auth and PostgREST"
  ]
}
JSON
python3 -c "import json;json.load(open('$WORK/MANIFEST.json'))" || die "manifest is not valid JSON"
MANIFEST_SHA="$(sha256sum "$WORK/MANIFEST.json" | cut -d' ' -f1)"

# ── 10. upload under .part names, verify remotely, then rename ──────────────
COMPONENTS=(db.dump.gpg globals.sql.gpg dbconfig.tar.gz.gpg rootkey.bin.gpg MANIFEST.json)

for remote in "${REMOTES[@]}"; do
  log "uploading to $remote"
  for c in "${COMPONENTS[@]}"; do
    $RC copyto "$WORK/$c" "$remote/$PREFIX/$c.part" -q || die "upload failed: $remote/$c"
  done
  # verify remote bytes before promoting any name
  for c in "${COMPONENTS[@]}"; do
    local_sha="$(sha256sum "$WORK/$c" | cut -d' ' -f1)"
    remote_sha="$($RC cat "$remote/$PREFIX/$c.part" | sha256sum | cut -d' ' -f1)"
    [[ "$local_sha" == "$remote_sha" ]] || die "remote digest mismatch for $c on $remote"
    local_size="$(stat -c %s "$WORK/$c")"
    remote_size="$($RC size "$remote/$PREFIX/$c.part" --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["bytes"])')"
    [[ "$local_size" == "$remote_size" ]] || die "remote size mismatch for $c on $remote"
  done
  for c in "${COMPONENTS[@]}"; do
    $RC moveto "$remote/$PREFIX/$c.part" "$remote/$PREFIX/$c" -q || die "promote failed: $remote/$c"
  done
  log "verified on $remote"
done

# ── 11. completion marker LAST, on both remotes ─────────────────────────────
cat > "$WORK/COMPLETE" <<EOF
set_id=$SETID
manifest_sha256=$MANIFEST_SHA
components=${#COMPONENTS[@]}
completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
for remote in "${REMOTES[@]}"; do
  $RC copyto "$WORK/COMPLETE" "$remote/$PREFIX/COMPLETE" -q || die "completion marker failed on $remote"
done

# ── 12. only now is this a success ──────────────────────────────────────────
date +%s > "$STATE"
echo "$SETID" > "$STATE_DIR/nt-recovery-set-last-id"
log "✔ recovery set $SETID published and verified on ${#REMOTES[@]} remotes"
[[ -s "$KUMA_FILE" ]] && curl -fsS -m 10 "$(tr -d '[:space:]' < "$KUMA_FILE")?status=up&msg=$SETID" >/dev/null 2>&1 || true
exit 0
