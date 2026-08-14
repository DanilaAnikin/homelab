#!/usr/bin/env bash
# Restore one authenticated Postiz recovery set independently from primary and DR.
# Remote reads run on the host. Every restored service/file parser container has
# Docker network `none`; no production volume, container, port or Compose project is used.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py
readonly OFFLINE_VERIFY=/srv/homelab/self-healing/postiz-offline-verify.sh
readonly BACKUP_KEY=/srv/homelab/secrets/freio-backup-key.txt
readonly R2_CONF=/srv/homelab/secrets/rclone.conf
readonly POLICY_ATTESTER=/usr/local/sbin/postiz-r2-policy-attest.sh
readonly STORAGE_POLICY=/var/lib/homelab-backup/postiz-storage-policy.json
readonly PRIMARY_ROOT=r2postiz:homelab-backups/postiz
readonly DR_ROOT=r2drpostiz:homelab-backups-dr/postiz
readonly STATE_ROOT=/var/lib/homelab-backup
readonly RUN_ROOT=/run/homelab-backup
readonly RESTORE_LOCK=$RUN_ROOT/postiz-restore.lock
readonly RESTORE_JOURNAL=$STATE_ROOT/postiz-restore-active.json
readonly MAX_RESTORE_PEAK_BYTES=$((192 * 1024 * 1024 * 1024))
readonly MAX_OPERATOR_ARCHIVE_BYTES=$((512 * 1024 * 1024))
readonly MAX_OPERATOR_CIPHER_BYTES=$((MAX_OPERATOR_ARCHIVE_BYTES + 16 * 1024 * 1024))
readonly MAX_PHYSICAL_EXPANDED_BYTES=$((24 * 1024 * 1024 * 1024))
readonly MAX_RUNTIME_CONFIG_EXPANDED_BYTES=$((64 * 1024 * 1024))
readonly MIN_FREE_MARGIN_BYTES=$((4 * 1024 * 1024 * 1024))
readonly MAX_CONFIG_TREE_MEMBERS=4096
readonly MAX_SEASONAL_TREE_MEMBERS=10000
readonly -a DATABASES=(postiz temporal temporal_visibility insights)
readonly -a IMAGE_SERVICES=(postiz postiz-postgres postiz-redis postiz-temporal)
readonly -a RESTORE_ROLES=(
  logical-primary physical-extract-primary physical-verify-primary physical-primary
  redis-check-primary redis-uid-primary redis-gid-primary redis-primary offline-primary
  logical-dr physical-extract-dr physical-verify-dr physical-dr
  redis-check-dr redis-uid-dr redis-gid-dr redis-dr offline-dr
)
readonly -a PARSER_LIMITS=(--memory 1g --memory-swap 1g --pids-limit 256 --cpus 1)
readonly -a POSTGRES_LIMITS=(--memory 4g --memory-swap 4g --pids-limit 512 --cpus 2)
readonly -a REDIS_LIMITS=(--memory 1g --memory-swap 1g --pids-limit 256 --cpus 1)
readonly -a PAYLOAD_KEYS=(
  physical_cluster capture_evidence globals database_postiz database_temporal
  database_temporal_visibility database_insights runtime_config config_volume
  redis artifacts operator_state storage_policy
)
readonly -a RC=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent
  rclone --config "$R2_CONF" --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G)

die() { printf 'Postiz restore drill: %s\n' "$*" >&2; exit 1; }
log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }
usage() { printf 'usage: %s [--cleanup-only]\n' "$0" >&2; exit 64; }

safe_root_file() {
  local path=$1 mode=$2
  [[ -f "$path" && ! -L "$path" && "$(stat -Lc '%u:%g:%a:%h' "$path")" == "0:0:${mode}:1" ]] \
    || die "trusted file contract failed: $path"
}

cleanup_only=0
case $# in
  0) ;;
  1) [[ "$1" == --cleanup-only ]] || usage; cleanup_only=1 ;;
  *) usage ;;
esac

((EUID == 0)) || die 'must run as root'
safe_root_file "$HELPER" 755
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || die 'backup StateDirectory is unsafe'
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || die 'backup RuntimeDirectory is unsafe'
safe_root_file "$RESTORE_LOCK" 600
command -v docker >/dev/null || die 'docker is missing'

exec 9<>"$RESTORE_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$RESTORE_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/9")" ]] \
  || die 'restore lock descriptor/path drifted'
flock -n 9 || die 'another Postiz restore drill is running'

reap_restore_state() {
  local stale_work run_id role container rows row listed_id listed_name listed_extra inspection
  local listed_role candidate basename container_count=0
  local -A expected_role_by_name=() listed_id_by_name=()
  if [[ -e "$RESTORE_JOURNAL" || -L "$RESTORE_JOURNAL" ]]; then
    safe_root_file "$RESTORE_JOURNAL" 600
    stale_work=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
      --key work_directory)
    run_id=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" --key run_id)
    [[ "$stale_work" == "$STATE_ROOT/postiz-restore.$run_id" && \
       "$run_id" =~ ^[A-Za-z0-9]{6}$ ]] || die 'restore cleanup journal path differs'
    for role in "${RESTORE_ROLES[@]}"; do
      container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
        --role "$role" --key container)
      [[ -z "${expected_role_by_name[$container]+x}" ]] \
        || die 'restore journal repeats a container name'
      expected_role_by_name[$container]=$role
    done
    rows=$(timeout --signal=TERM --kill-after=5s 30s docker ps -a --no-trunc \
      --filter "name=^/postiz-restore-${run_id}-" \
      --format '{{.ID}}|{{.Names}}' 2>/dev/null) \
      || die 'cannot prove restore container presence/absence'
    ((${#rows} <= 16384)) || die 'restore container listing exceeds byte ceiling'
    while IFS= read -r row; do
      [[ -n "$row" ]] || continue
      IFS='|' read -r listed_id listed_name listed_extra <<< "$row"
      listed_role=${expected_role_by_name[$listed_name]:-}
      [[ "$listed_id" =~ ^[0-9a-f]{64}$ && -n "$listed_role" && \
         -z "$listed_extra" && -z "${listed_id_by_name[$listed_name]+x}" ]] \
        || die 'restore container listing is invalid or unexpected'
      listed_id_by_name[$listed_name]=$listed_id
      container_count=$((container_count + 1))
      ((container_count <= ${#RESTORE_ROLES[@]})) \
        || die 'restore container listing exceeds entry ceiling'
    done <<< "$rows"
    for role in "${RESTORE_ROLES[@]}"; do
      container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
        --role "$role" --key container)
      if [[ -n "${listed_id_by_name[$container]+x}" ]]; then
        listed_id=${listed_id_by_name[$container]}
        inspection=$(timeout --signal=TERM --kill-after=5s 30s docker inspect --format \
          '{{.Name}}|{{with .Config.Labels}}{{index . "freio.postiz.restore-run"}}{{end}}|{{with .Config.Labels}}{{index . "freio.postiz.restore-role"}}{{end}}' \
          "$listed_id" 2>/dev/null) || die 'cannot inspect restore container'
        [[ "$inspection" == "/$container|$run_id|$role" ]] \
          || die 'restore cleanup container identity/label differs'
        timeout --signal=TERM --kill-after=10s 30s docker rm -f "$listed_id" >/dev/null \
          || die 'restore cleanup could not remove a drill container'
      fi
    done
    if [[ -e "$stale_work" || -L "$stale_work" ]]; then
      [[ -d "$stale_work" && ! -L "$stale_work" && \
         "$(stat -Lc '%u:%g:%a' "$stale_work")" == 0:0:700 ]] \
        || die 'restore cleanup work directory is unsafe'
      rm -rf --one-file-system -- "$stale_work"
    fi
    rm -f -- "$RESTORE_JOURNAL"
    sync -f "$STATE_ROOT"
  fi
  shopt -s nullglob
  for candidate in "$STATE_ROOT"/postiz-restore.??????; do
    basename=${candidate##*/}
    [[ "$basename" =~ ^postiz-restore\.[A-Za-z0-9]{6}$ && \
       -d "$candidate" && ! -L "$candidate" && \
       "$(stat -Lc '%u:%g:%a' "$candidate")" == 0:0:700 ]] \
      || die 'orphan restore workspace is unsafe'
    rm -rf --one-file-system -- "$candidate"
  done
  shopt -u nullglob
}

reap_restore_state
((cleanup_only)) && exit 0

safe_root_file "$OFFLINE_VERIFY" 750
safe_root_file "$BACKUP_KEY" 600
safe_root_file "$R2_CONF" 600
safe_root_file "$POLICY_ATTESTER" 755
policy_attester_rc=0
"$POLICY_ATTESTER" || policy_attester_rc=$?
if ((policy_attester_rc == 0)); then
  safe_root_file "$STORAGE_POLICY" 600
  "$HELPER" verify-storage-policy --policy "$STORAGE_POLICY"
elif ((policy_attester_rc == 75)); then
  log 'current R2 policy API transport is unavailable; requiring authenticated historical policy evidence'
else
  die 'current R2 retention policy, credential scope, or local attestation contract is invalid'
fi

work=$(mktemp -d "$STATE_ROOT/postiz-restore.XXXXXX")
run_id=${work##*.}
"$HELPER" write-restore-journal --timestamp "$(date -u +%Y%m%dT%H%M%SZ)" \
  --run-id "$run_id" --output "$RESTORE_JOURNAL"

cleanup() { reap_restore_state; }
on_exit() {
  local rc=$?
  trap - EXIT
  cleanup || rc=1
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

primary_commits=$work/primary-commits.txt
dr_commits=$work/dr-commits.txt
install -m 600 /dev/null "$primary_commits"
install -m 600 /dev/null "$dr_commits"

list_commits() {
  local remote=$1 output=$2 listing=$work/commit-listing-$3.txt relative month timestamp
  local lines invalid=0
  (
    ulimit -f 10240
    "${RC[@]}" lsf "$remote/recovery-sets" --recursive --files-only \
      --include '*/COMMITTED.hmac.json' > "$listing"
  ) || die 'committed-set listing failed or exceeded its byte ceiling'
  lines=$(wc -l < "$listing")
  [[ "$lines" =~ ^[0-9]+$ && "$lines" -le 10000 ]] \
    || die 'committed-set listing exceeds entry ceiling'
  while IFS= read -r relative; do
    [[ -z "$relative" ]] && continue
    if [[ ! "$relative" =~ ^([0-9]{4}-[0-9]{2})/([0-9]{8}T[0-9]{6}Z)/COMMITTED\.hmac\.json$ ]]; then
      invalid=$((invalid + 1))
      continue
    fi
    month=${BASH_REMATCH[1]}
    timestamp=${BASH_REMATCH[2]}
    if [[ "$month" != "${timestamp:0:4}-${timestamp:4:2}" ]]; then
      invalid=$((invalid + 1))
      continue
    fi
    printf '%s/%s\n' "$month" "$timestamp" >> "$output"
  done < "$listing"
  sort -u -o "$output" "$output"
  ((invalid == 0)) || log "$3 ignored $invalid non-canonical commit-key candidate(s)"
}

list_commits "$PRIMARY_ROOT" "$primary_commits" primary
list_commits "$DR_ROOT" "$dr_commits" dr

fetch_bounded() {
  local remote_path=$1 destination=$2 max_bytes=$3 size directory basename listing line listed_name
  directory=$(dirname -- "$remote_path")
  basename=$(basename -- "$remote_path")
  listing=$work/preflight-$(printf '%s' "$remote_path" | sha256sum | cut -d' ' -f1).txt
  "${RC[@]}" lsf "$directory" --files-only --include "$basename" \
    --format sp --separator '|' > "$listing"
  [[ "$(wc -l < "$listing" | tr -d '[:space:]')" == 1 ]] \
    || die 'remote object is absent or ambiguous during size preflight'
  line=$(<"$listing")
  IFS='|' read -r size listed_name <<< "$line"
  [[ "$size" =~ ^[0-9]+$ && "$listed_name" == "$basename" ]] \
    || die 'remote object size preflight is invalid'
  ((size > 0 && size <= max_bytes)) || die 'remote object exceeds its byte ceiling before fetch'
  (
    ulimit -f $(((max_bytes + 1023) / 1024))
    timeout --signal=TERM --kill-after=10s 1800s \
      "${RC[@]}" copyto "$remote_path" "$destination" -q
  ) || die 'bounded remote object fetch failed'
  [[ -f "$destination" && ! -L "$destination" ]] || die 'download did not produce a regular file'
  [[ "$(stat -Lc '%s' "$destination")" == "$size" ]] \
    || die 'downloaded object size differs from remote preflight'
}

fetch_checked() {
  local remote_path=$1 destination=$2 expected_sha=$3 expected_bytes=$4 actual
  [[ "$expected_sha" =~ ^[0-9a-f]{64}$ && "$expected_bytes" =~ ^[0-9]+$ ]] \
    || die 'invalid authenticated payload metadata'
  fetch_bounded "$remote_path" "$destination" "$expected_bytes"
  [[ "$(stat -Lc '%s' "$destination")" == "$expected_bytes" ]] \
    || die 'downloaded ciphertext size differs'
  actual=$(sha256sum "$destination" | cut -d' ' -f1)
  [[ "$actual" == "$expected_sha" ]] || die 'downloaded ciphertext digest differs'
}

decrypt_file() {
  local source=$1 destination=$2 source_bytes
  source_bytes=$(stat -Lc '%s' "$source")
  [[ "$source_bytes" =~ ^[0-9]+$ && "$source_bytes" -gt 0 ]] \
    || die 'authenticated ciphertext size is invalid'
  (
    ulimit -f $(((source_bytes + 1023) / 1024))
    timeout --signal=TERM --kill-after=10s 1800s \
      openssl enc -d -aes-256-cbc -pbkdf2 -in "$source" -out "$destination" \
        -pass file:"$BACKUP_KEY" 2>/dev/null
  ) || die 'authenticated payload did not decrypt'
  [[ -s "$destination" ]] || die 'decryption produced an empty payload'
  chmod 600 "$destination"
}

marker_get() {
  "$HELPER" recovery-get --recovery-set "$1" --key "$2"
}

capture_get() {
  "$HELPER" capture-get --evidence "$1" --key "$2"
}

# Validate HMAC/context on both remotes while selecting.  A newer append-only
# junk/replay candidate is skipped rather than permanently masking an older
# valid committed set.
set_relative=
while IFS= read -r candidate; do
  grep -Fxq -- "$candidate" "$dr_commits" || continue
  candidate_timestamp=${candidate#*/}
  candidate_root=$work/candidate-$candidate_timestamp
  mkdir -m 700 "$candidate_root" "$candidate_root/primary" "$candidate_root/dr"
  if (
    fetch_bounded "$PRIMARY_ROOT/recovery-sets/$candidate/COMMITTED.hmac.json" \
      "$candidate_root/primary/COMMITTED.hmac.json" $((1024 * 1024)) &&
    fetch_bounded "$PRIMARY_ROOT/recovery-sets/$candidate/recovery-set.json.enc" \
      "$candidate_root/primary/recovery-set.json.enc" $((64 * 1024 * 1024)) &&
    fetch_bounded "$DR_ROOT/recovery-sets/$candidate/COMMITTED.hmac.json" \
      "$candidate_root/dr/COMMITTED.hmac.json" $((1024 * 1024)) &&
    fetch_bounded "$DR_ROOT/recovery-sets/$candidate/recovery-set.json.enc" \
      "$candidate_root/dr/recovery-set.json.enc" $((64 * 1024 * 1024)) &&
    cmp -s "$candidate_root/primary/COMMITTED.hmac.json" \
      "$candidate_root/dr/COMMITTED.hmac.json" &&
    cmp -s "$candidate_root/primary/recovery-set.json.enc" \
      "$candidate_root/dr/recovery-set.json.enc" &&
    "$HELPER" verify-auth-record --cipher "$candidate_root/primary/recovery-set.json.enc" \
      --record "$candidate_root/primary/COMMITTED.hmac.json" --key-file "$BACKUP_KEY" \
      --expected-context "postiz-recovery-set:$candidate_timestamp" &&
    "$HELPER" verify-auth-record --cipher "$candidate_root/dr/recovery-set.json.enc" \
      --record "$candidate_root/dr/COMMITTED.hmac.json" --key-file "$BACKUP_KEY" \
      --expected-context "postiz-recovery-set:$candidate_timestamp"
  ) >/dev/null 2>&1; then
    set_relative=$candidate
    break
  fi
  log "ignored invalid/replayed common committed-set candidate $candidate_timestamp"
  rm -rf --one-file-system -- "$candidate_root"
done < <(sort -r "$primary_commits")
[[ -n "$set_relative" ]] || die 'no identical authenticated Postiz set exists on primary and DR'
set_timestamp=${set_relative#*/}
log "selected authenticated Postiz recovery set $set_timestamp"

sql_scalar() {
  local container=$1 user=$2 database=$3 query=$4 result
  result=$(timeout --signal=TERM --kill-after=5s 30s docker exec \
    -e PGOPTIONS='-c statement_timeout=25000 -c lock_timeout=3000 -c TimeZone=UTC -c DateStyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3' \
    "$container" psql -X -v ON_ERROR_STOP=1 -U "$user" -d "$database" \
    -Atc "$query" 2>/dev/null) || die "restored database query failed: $database"
  result=${result//[[:space:]]/}
  [[ "$result" =~ ^[0-9]+$ ]] || die 'restored database query is not numeric'
  printf '%s\n' "$result"
}

sql_fingerprint() {
  local container=$1 user=$2 database=$3 query=$4 digest
  digest=$(timeout --signal=TERM --kill-after=5s 45s docker exec \
    -e PGOPTIONS='-c statement_timeout=40000 -c lock_timeout=3000 -c TimeZone=UTC -c DateStyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3' \
    "$container" psql -X -v ON_ERROR_STOP=1 -U "$user" -d "$database" -Atc "$query" \
    2>/dev/null | sha256sum | cut -d' ' -f1) \
    || die "restored database fingerprint failed: $database"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die 'restored database fingerprint is invalid'
  printf '%s\n' "$digest"
}

assert_equal_count() {
  local evidence=$1 key=$2 container=$3 user=$4 database=$5 query=$6 actual expected
  actual=$(sql_scalar "$container" "$user" "$database" "$query")
  expected=$(capture_get "$evidence" "$key")
  [[ "$actual" == "$expected" ]] || die "restored invariant differs: $key"
}

assert_database_set() {
  local evidence=$1 container=$2 user=$3 logical_extra_role=$4
  local inventory objects expected_roles actual_roles actual expected query database migration_name
  inventory=$(docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U "$user" -d postgres -Atc \
    "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1" 2>/dev/null)
  [[ "$inventory" == $'insights\npostgres\npostiz\ntemporal\ntemporal_visibility' ]] \
    || die 'restored non-template database inventory differs'
  objects=$(sql_scalar "$container" "$user" postgres "
    SELECT
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')
      +
      (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')")
  [[ "$objects" == 0 ]] || die 'restored maintenance postgres DB has user objects'
  expected_roles=$(capture_get "$evidence" cluster_roles)
  actual_roles=$(sql_scalar "$container" "$user" postgres 'SELECT count(*) FROM pg_roles')
  [[ "$actual_roles" == $((expected_roles + logical_extra_role)) ]] \
    || die 'restored cluster role count differs'
  assert_equal_count "$evidence" cluster_role_memberships "$container" "$user" postgres \
    'SELECT count(*) FROM pg_auth_members'
  assert_equal_count "$evidence" postiz_role_superuser "$container" "$user" postgres \
    "SELECT count(*) FROM pg_roles WHERE rolname='postiz' AND rolsuper"
  assert_equal_count "$evidence" postiz_role_login "$container" "$user" postgres \
    "SELECT count(*) FROM pg_roles WHERE rolname='postiz' AND rolcanlogin"
  assert_equal_count "$evidence" postiz_public_tables "$container" "$user" postiz \
    "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
  assert_equal_count "$evidence" postiz_posts "$container" "$user" postiz \
    'SELECT count(*) FROM "Post"'
  assert_equal_count "$evidence" postiz_integrations "$container" "$user" postiz \
    'SELECT count(*) FROM "Integration"'
  assert_equal_count "$evidence" temporal_tables "$container" "$user" temporal \
    "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
  assert_equal_count "$evidence" temporal_executions "$container" "$user" temporal \
    'SELECT count(*) FROM executions'
  assert_equal_count "$evidence" temporal_current_executions "$container" "$user" temporal \
    'SELECT count(*) FROM current_executions'
  assert_equal_count "$evidence" temporal_tasks "$container" "$user" temporal \
    'SELECT count(*) FROM tasks'
  assert_equal_count "$evidence" temporal_schema_versions "$container" "$user" temporal \
    'SELECT count(*) FROM schema_version'
  assert_equal_count "$evidence" visibility_tables "$container" "$user" temporal_visibility \
    "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
  assert_equal_count "$evidence" visibility_executions "$container" "$user" temporal_visibility \
    'SELECT count(*) FROM executions_visibility'
  assert_equal_count "$evidence" visibility_schema_versions "$container" "$user" temporal_visibility \
    'SELECT count(*) FROM schema_version'
  assert_equal_count "$evidence" insights_tables "$container" "$user" insights \
    "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
  assert_equal_count "$evidence" insights_post_insights "$container" "$user" insights \
    'SELECT count(*) FROM post_insights'
  assert_equal_count "$evidence" insights_farm_pr_reviews "$container" "$user" insights \
    'SELECT count(*) FROM farm_pr_reviews'
  assert_equal_count "$evidence" insights_farm_pr_triage "$container" "$user" insights \
    'SELECT count(*) FROM farm_pr_triage'
  [[ "$(sql_scalar "$container" "$user" postiz \
    "SELECT CASE WHEN to_regclass('public.\"_prisma_migrations\"') IS NULL THEN 0 ELSE 1 END")" == 0 ]] \
    || die 'restored Postiz Prisma migration-table absence contract drifted'

  query=$("$HELPER" emit-fingerprint-sql --kind roles)
  actual=$(sql_fingerprint "$container" "$user" postgres "$query")
  expected=$(capture_get "$evidence" roles)
  [[ "$actual" == "$expected" ]] || die 'restored role attributes fingerprint differs'
  query=$("$HELPER" emit-fingerprint-sql --kind role_memberships)
  actual=$(sql_fingerprint "$container" "$user" postgres "$query")
  expected=$(capture_get "$evidence" role_memberships)
  [[ "$actual" == "$expected" ]] || die 'restored role-membership fingerprint differs'
  for database in "${DATABASES[@]}"; do
    query=$("$HELPER" emit-fingerprint-sql --kind catalog)
    actual=$(sql_fingerprint "$container" "$user" "$database" "$query")
    expected=$(capture_get "$evidence" "catalog_$database")
    [[ "$actual" == "$expected" ]] \
      || die "restored owner/ACL/extension catalog fingerprint differs: $database"
  done
  for migration_spec in \
      'temporal_schema_version|temporal' \
      'temporal_visibility_schema_version|temporal_visibility'; do
    IFS='|' read -r migration_name database <<< "$migration_spec"
    query=$("$HELPER" emit-fingerprint-sql --kind migration \
      --name "$migration_name" --database "$database")
    actual=$(sql_fingerprint "$container" "$user" "$database" "$query")
    expected=$(capture_get "$evidence" "migration_$migration_name")
    [[ "$actual" == "$expected" ]] \
      || die "restored schema-migration fingerprint differs: $database"
  done
}

wait_postgres() {
  local container=$1 user=$2
  for _ in $(seq 1 90); do
    docker exec "$container" pg_isready -U "$user" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

restore_logical_databases() {
  local base=$1 label=$2 image_id=$3 evidence=$4 database
  local role="logical-$label" container
  local -a run_args=()
  container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$role" --key container)
  mkdir -m 700 "$base/logical-data"
  run_args=(docker run -d --name "$container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$role" \
    "${POSTGRES_LIMITS[@]}" --network none --read-only \
    -v "$base/logical-data:/var/lib/postgresql/data:rw" \
    -v "$base/plain/globals.sql:/restore/globals.sql:ro" \
    --tmpfs /var/run/postgresql:rw,nosuid,nodev,size=32m \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    -e POSTGRES_USER=freio_restore_bootstrap -e POSTGRES_PASSWORD=offline-drill-only \
    -e POSTGRES_DB=postgres)
  for database in "${DATABASES[@]}"; do
    run_args+=(-v "$base/plain/database-$database.dump:/restore/database-$database.dump:ro")
  done
  run_args+=("$image_id")
  "${run_args[@]}" >/dev/null
  wait_postgres "$container" freio_restore_bootstrap \
    || die 'logical restore Postgres did not become ready'
  timeout --signal=TERM --kill-after=10s 120s docker exec "$container" \
    psql -X -v ON_ERROR_STOP=1 -U freio_restore_bootstrap -d postgres \
      -f /restore/globals.sql >/dev/null 2>&1 || die 'strict globals restore failed'
  for database in "${DATABASES[@]}"; do
    docker exec "$container" createdb -U freio_restore_bootstrap -O postiz "$database" >/dev/null
    timeout --signal=TERM --kill-after=30s 1200s docker exec "$container" \
      pg_restore --exit-on-error -U freio_restore_bootstrap \
      -d "$database" "/restore/database-$database.dump" >/dev/null 2>&1 \
      || die "strict logical restore failed: $database"
  done
  assert_database_set "$evidence" "$container" freio_restore_bootstrap 1
  docker rm -f "$container" >/dev/null
  log "$label strict globals + four logical databases OK"
}

restore_physical_cluster() {
  local base=$1 label=$2 image_id=$3 evidence=$4
  local role="physical-$label" extract_role="physical-extract-$label"
  local verify_role="physical-verify-$label" container extract_container verify_container
  container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$role" --key container)
  extract_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$extract_role" --key container)
  verify_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$verify_role" --key container)
  mkdir -m 700 "$base/physical-data"
  "$HELPER" verify-physical-archive --archive "$base/plain/postgres-cluster.tar.gz" \
    --max-bytes $((24 * 1024 * 1024 * 1024))
  docker run --rm --name "$extract_container" \
    --label "freio.postiz.restore-run=$run_id" \
    --label "freio.postiz.restore-role=$extract_role" \
    "${PARSER_LIMITS[@]}" --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    -v "$base/plain/postgres-cluster.tar.gz:/drill/cluster.tar.gz:ro" \
    -v "$base/physical-data:/restore:rw" --entrypoint tar "$image_id" \
    --no-same-owner -xzf /drill/cluster.tar.gz -C /restore >/dev/null
  timeout --signal=TERM --kill-after=15s 300s docker run --rm --name "$verify_container" \
    --label "freio.postiz.restore-run=$run_id" \
    --label "freio.postiz.restore-role=$verify_role" \
    "${PARSER_LIMITS[@]}" --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    -v "$base/physical-data:/restore:ro" --entrypoint pg_verifybackup \
    "$image_id" /restore >/dev/null 2>&1 \
    || die 'pg_verifybackup rejected the physical Postgres capture'
  docker run -d --name "$container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$role" \
    "${POSTGRES_LIMITS[@]}" --network none --read-only \
    -v "$base/physical-data:/var/lib/postgresql/data:rw" \
    --tmpfs /var/run/postgresql:rw,nosuid,nodev,size=32m \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g "$image_id" >/dev/null
  wait_postgres "$container" postiz || die 'physical recovery Postgres did not become ready'
  assert_database_set "$evidence" "$container" postiz 0
  docker rm -f "$container" >/dev/null
  log "$label WAL-consistent physical four-database cluster OK"
}

restore_redis() {
  local base=$1 label=$2 image_id=$3 evidence=$4 uid gid expected
  local check_output rdb_keys persistence loading loaded expired
  local root_uid root_gid root_mode rdb_uid rdb_gid rdb_mode
  local role="redis-$label" check_role="redis-check-$label"
  local uid_role="redis-uid-$label" gid_role="redis-gid-$label"
  local container check_container uid_container gid_container
  container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$role" --key container)
  check_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$check_role" --key container)
  uid_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$uid_role" --key container)
  gid_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$gid_role" --key container)
  check_output=$base/redis-check-rdb.txt
  docker run --rm --name "$check_container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$check_role" \
    "${PARSER_LIMITS[@]}" --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    -v "$base/plain/redis.rdb:/backup/dump.rdb:ro" --entrypoint redis-check-rdb \
    "$image_id" /backup/dump.rdb > "$check_output" 2>&1 || die 'Redis RDB integrity check failed'
  rdb_keys=$(sed -nE 's/^\[info\] ([0-9]+) keys read$/\1/p' "$check_output")
  expected=$(capture_get "$evidence" redis_rdb_keys)
  [[ "$rdb_keys" =~ ^[0-9]+$ && "$rdb_keys" == "$expected" ]] \
    || die 'restored Redis RDB structural key count differs'
  rm -f -- "$check_output"
  mkdir -m 700 "$base/redis-data"
  cp -- "$base/plain/redis.rdb" "$base/redis-data/dump.rdb"
  uid=$(docker run --rm --name "$uid_container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$uid_role" \
    "${PARSER_LIMITS[@]}" --network none --read-only --entrypoint sh \
    "$image_id" -c 'id -u redis')
  gid=$(docker run --rm --name "$gid_container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$gid_role" \
    "${PARSER_LIMITS[@]}" --network none --read-only --entrypoint sh \
    "$image_id" -c 'id -g redis')
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ ]] || die 'archived Redis UID/GID is invalid'
  root_uid=$(capture_get "$evidence" redis_root_uid)
  root_gid=$(capture_get "$evidence" redis_root_gid)
  root_mode=$(capture_get "$evidence" redis_root_mode)
  rdb_uid=$(capture_get "$evidence" redis_rdb_uid)
  rdb_gid=$(capture_get "$evidence" redis_rdb_gid)
  rdb_mode=$(capture_get "$evidence" redis_rdb_mode)
  [[ "$root_uid" =~ ^[0-9]+$ && "$root_gid" =~ ^[0-9]+$ && \
     "$rdb_uid" == "$uid" && "$rdb_gid" == "$gid" && \
     "$root_mode" =~ ^[0-7]{3,4}$ && "$rdb_mode" =~ ^[0-7]{3,4}$ ]] \
    || die 'captured Redis owner/mode differs from the archived image contract'
  chown "$root_uid:$root_gid" "$base/redis-data"
  chmod "$root_mode" "$base/redis-data"
  chown "$rdb_uid:$rdb_gid" "$base/redis-data/dump.rdb"
  chmod "$rdb_mode" "$base/redis-data/dump.rdb"
  docker run -d --name "$container" \
    --label "freio.postiz.restore-run=$run_id" --label "freio.postiz.restore-role=$role" \
    "${REDIS_LIMITS[@]}" --network none --read-only \
    -v "$base/redis-data:/data:rw" --tmpfs /tmp:rw,nosuid,nodev,size=32m \
    "$image_id" redis-server --dir /data --dbfilename dump.rdb --appendonly no --save '' >/dev/null
  for _ in $(seq 1 60); do
    [[ "$(docker exec "$container" redis-cli ping 2>/dev/null || true)" == PONG ]] && break
    sleep 1
  done
  [[ "$(docker exec "$container" redis-cli ping 2>/dev/null || true)" == PONG ]] \
    || die 'restored Redis did not become ready'
  persistence=$(docker exec "$container" redis-cli INFO persistence 2>/dev/null)
  loading=$(sed -nE 's/^loading:([0-9]+)\r?$/\1/p' <<< "$persistence")
  loaded=$(sed -nE 's/^rdb_last_load_keys_loaded:([0-9]+)\r?$/\1/p' <<< "$persistence")
  expired=$(sed -nE 's/^rdb_last_load_keys_expired:([0-9]+)\r?$/\1/p' <<< "$persistence")
  [[ "$loading" == 0 && "$loaded" =~ ^[0-9]+$ && "$expired" =~ ^[0-9]+$ && \
     $((loaded + expired)) -eq "$expected" ]] \
    || die 'restored Redis TTL-aware load accounting differs from captured RDB'
  docker rm -f "$container" >/dev/null
  log "$label Redis RDB restore OK ($loaded loaded, $expired expired by absolute TTL)"
}

restore_one_remote() {
  local label=$1 remote=$2
  local base=$work/$label
  local set_root=$remote/recovery-sets/$set_relative
  local marker_cipher=$base/recovery-set.json.enc marker_json=$base/recovery-set.json
  local commit=$base/COMMITTED.hmac.json payload_total=0 key bytes filename sha
  local artifacts evidence operator historical_policy upload_count upload_bytes upload_cipher_bytes
  local max_upload_cipher_bytes upload_transfer_cap manifest_key manifest_sha
  local manifest_cipher manifest_json image_total=0 image_expanded_total=0 image_inode_total=0
  local peak free_bytes free_inodes required_inodes
  local docker_root docker_free_bytes docker_free_inodes current_image_id
  local service image_id configured_ref image_key image_sha image_bytes image_uncompressed_bytes
  local image_uncompressed_inodes image_cipher image_plain image_expanded_stats image_inode_stats
  local operator_present=0 status archive_name archive_sha archive_cipher archive_plain
  local compose_images receipt_images recovery_override recovery_ids
  local offline_role="offline-$label" offline_container
  declare -A image_ids=()
  declare -A configured_refs=()
  offline_container=$("$HELPER" restore-journal-get --journal "$RESTORE_JOURNAL" \
    --role "$offline_role" --key container)
  mkdir -m 700 "$base" "$base/cipher" "$base/plain" "$base/uploads" "$base/upload-cipher" \
    "$base/image-cipher" "$base/images" "$base/offline-output"

  fetch_bounded "$set_root/COMMITTED.hmac.json" "$commit" $((1024 * 1024))
  fetch_bounded "$set_root/recovery-set.json.enc" "$marker_cipher" $((64 * 1024 * 1024))
  "$HELPER" verify-auth-record --cipher "$marker_cipher" --record "$commit" \
    --key-file "$BACKUP_KEY" --expected-context "postiz-recovery-set:$set_timestamp"
  decrypt_file "$marker_cipher" "$marker_json"
  [[ "$(marker_get "$marker_json" created_at)" == "$set_timestamp" ]] \
    || die 'recovery marker timestamp differs from committed path'

  for key in "${PAYLOAD_KEYS[@]}"; do
    bytes=$(marker_get "$marker_json" "${key}_cipher_bytes")
    payload_total=$((payload_total + bytes))
  done

  fetch_marker_payload() {
    local payload_key=$1 plain_name=$2 expected_name=$3
    local remote_name remote_sha remote_bytes cipher_path plain_path
    remote_name=$(marker_get "$marker_json" "${payload_key}_filename")
    remote_sha=$(marker_get "$marker_json" "${payload_key}_cipher_sha256")
    remote_bytes=$(marker_get "$marker_json" "${payload_key}_cipher_bytes")
    [[ "$remote_name" == "$expected_name" ]] || die "unexpected recovery payload filename: $payload_key"
    cipher_path=$base/cipher/$remote_name
    plain_path=$base/plain/$plain_name
    fetch_checked "$set_root/$remote_name" "$cipher_path" "$remote_sha" "$remote_bytes"
    decrypt_file "$cipher_path" "$plain_path"
  }

  fetch_marker_payload artifacts artifacts.json "postiz_artifacts_$set_timestamp.json.enc"
  fetch_marker_payload capture_evidence capture.evidence.json "postiz_capture_$set_timestamp.evidence.json.enc"
  fetch_marker_payload operator_state operator-state.json "postiz_operator_state_$set_timestamp.json.enc"
  fetch_marker_payload storage_policy storage-policy.json "postiz_storage_policy_$set_timestamp.json.enc"
  artifacts=$base/plain/artifacts.json
  evidence=$base/plain/capture.evidence.json
  operator=$base/plain/operator-state.json
  historical_policy=$base/plain/storage-policy.json
  "$HELPER" verify-storage-policy --policy "$historical_policy" --historical
  [[ "$("$HELPER" artifact-get --receipt "$artifacts" --key created_at)" == "$set_timestamp" ]] \
    || die 'artifact receipt timestamp differs'
  [[ "$(capture_get "$evidence" created_at)" == "$set_timestamp" ]] \
    || die 'capture evidence timestamp differs'

  upload_count=$("$HELPER" artifact-get --receipt "$artifacts" --key upload_file_count)
  upload_bytes=$("$HELPER" artifact-get --receipt "$artifacts" --key upload_total_bytes)
  manifest_key=$("$HELPER" artifact-get --receipt "$artifacts" --key upload_manifest_key)
  manifest_sha=$("$HELPER" artifact-get --receipt "$artifacts" --key upload_manifest_cipher_sha256)
  [[ "$manifest_key" == "uploads/manifests/${set_timestamp:0:4}-${set_timestamp:4:2}/uploads-$set_timestamp.json.enc" ]] \
    || die 'upload manifest key differs from committed timestamp'
  manifest_cipher=$base/cipher/uploads.json.enc
  manifest_json=$base/plain/uploads.json
  fetch_bounded "$remote/$manifest_key" "$manifest_cipher" $((128 * 1024 * 1024))
  [[ "$(sha256sum "$manifest_cipher" | cut -d' ' -f1)" == "$manifest_sha" ]] \
    || die 'upload manifest ciphertext digest differs'
  decrypt_file "$manifest_cipher" "$manifest_json"
  [[ "$(sha256sum "$manifest_json" | cut -d' ' -f1)" == \
     "$(capture_get "$evidence" upload_manifest_sha256)" ]] \
    || die 'upload manifest differs from writer-fenced capture evidence'
  IFS=$'\t' read -r declared_count declared_bytes < <("$HELPER" summary --manifest "$manifest_json")
  [[ "$declared_count" == "$upload_count" && "$declared_bytes" == "$upload_bytes" ]] \
    || die 'upload manifest totals differ from artifact receipt'
  "$HELPER" emit-blob-list --manifest "$manifest_json" --output "$base/blob-list.txt"
  "$HELPER" emit-blob-sizes --manifest "$manifest_json" --output "$base/blob-sizes.txt"
  "$HELPER" emit-checksums --manifest "$manifest_json" --output "$base/checksums.txt"
  upload_cipher_bytes=$(awk -F'|' '
    $1 !~ /^[0-9]+$/ { exit 2 }
    { total += $1 }
    END { if (NR == 0) print 0; else printf "%.0f\n", total }
  ' "$base/blob-sizes.txt") || die 'upload ciphertext total is invalid'
  max_upload_cipher_bytes=$((upload_bytes + upload_count * 32))
  upload_transfer_cap=$((max_upload_cipher_bytes + 1024 * 1024))
  [[ "$upload_cipher_bytes" =~ ^[0-9]+$ ]] \
    || die 'upload ciphertext total is not numeric'
  ((upload_cipher_bytes <= max_upload_cipher_bytes)) \
    || die 'upload ciphertext total exceeds manifest-derived ceiling'

  for service in "${IMAGE_SERVICES[@]}"; do
    image_bytes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_cipher_bytes)
    image_total=$((image_total + image_bytes))
    image_uncompressed_bytes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_uncompressed_bytes)
    image_expanded_total=$((image_expanded_total + image_uncompressed_bytes))
    image_uncompressed_inodes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_uncompressed_inodes)
    image_inode_total=$((image_inode_total + image_uncompressed_inodes))
  done
  peak=$((
    payload_total * 2
    + upload_transfer_cap + upload_bytes
    + image_total * 2
    + MAX_PHYSICAL_EXPANDED_BYTES * 2
    + MAX_RUNTIME_CONFIG_EXPANDED_BYTES
    + MAX_OPERATOR_ARCHIVE_BYTES * 6
    + 2 * 1024 * 1024 * 1024
    + MIN_FREE_MARGIN_BYTES
  ))
  ((peak <= MAX_RESTORE_PEAK_BYTES)) || die 'declared restore peak exceeds hard byte ceiling'
  required_inodes=$((upload_count * 2 + 2200000))
  free_bytes=$(df -PB1 --output=avail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
  free_inodes=$(df -Pi --output=iavail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
  [[ "$free_bytes" =~ ^[0-9]+$ && "$free_inodes" =~ ^[0-9]+$ ]] \
    || die 'cannot measure restore workspace capacity'
  docker_root=$(docker info --format '{{.DockerRootDir}}')
  [[ "$docker_root" == /* && -d "$docker_root" && ! -L "$docker_root" ]] \
    || die 'Docker data root is unsafe'
  docker_free_bytes=$(df -PB1 --output=avail "$docker_root" | tail -1 | tr -d '[:space:]')
  docker_free_inodes=$(df -Pi --output=iavail "$docker_root" | tail -1 | tr -d '[:space:]')
  [[ "$docker_free_bytes" =~ ^[0-9]+$ && "$docker_free_inodes" =~ ^[0-9]+$ ]] \
    || die 'cannot measure Docker image-store capacity'
  if [[ "$(stat -Lc '%d' "$STATE_ROOT")" == "$(stat -Lc '%d' "$docker_root")" ]]; then
    ((free_bytes >= peak + image_expanded_total && \
      free_inodes >= required_inodes + image_inode_total)) \
      || die 'shared restore/image filesystem byte/inode preflight failed before large fetch'
  else
    ((free_bytes >= peak && free_inodes >= required_inodes)) \
      || die 'restore workspace byte/inode preflight failed before large fetch'
    ((docker_free_bytes >= image_expanded_total + MIN_FREE_MARGIN_BYTES && \
      docker_free_inodes >= image_inode_total)) \
      || die 'Docker image-store byte/inode preflight failed'
  fi

  fetch_marker_payload physical_cluster postgres-cluster.tar.gz \
    "postiz_postgres_cluster_$set_timestamp.tar.gz.enc"
  [[ "$(sha256sum "$base/plain/postgres-cluster.tar.gz" | cut -d' ' -f1)" == \
     "$(capture_get "$evidence" physical_cluster_sha256)" ]] \
    || die 'physical Postgres archive differs from writer-fenced capture evidence'
  fetch_marker_payload globals globals.sql "globals_postiz-postgres_$set_timestamp.sql.enc"
  for database in "${DATABASES[@]}"; do
    fetch_marker_payload "database_${database}" "database-$database.dump" \
      "db_postiz-postgres_${database}_$set_timestamp.dump.enc"
  done
  fetch_marker_payload runtime_config runtime-config.tar.gz \
    "postiz_config_$set_timestamp.tar.gz.enc"
  fetch_marker_payload config_volume config-volume.tar.gz \
    "postiz_config_volume_$set_timestamp.tar.gz.enc"
  fetch_marker_payload redis redis.rdb "postiz_redis_$set_timestamp.rdb.enc"

  "$HELPER" verify-config-archive --archive "$base/plain/runtime-config.tar.gz" \
    --compose-sha256 "$("$HELPER" artifact-get --receipt "$artifacts" --key compose_sha256)" \
    --dockerfile-sha256 "$("$HELPER" artifact-get --receipt "$artifacts" --key dockerfile_sha256)"
  "$HELPER" verify-tree-archive --archive "$base/plain/config-volume.tar.gz" \
    --prefix postiz-config --max-bytes $((64 * 1024 * 1024)) \
    --max-members "$MAX_CONFIG_TREE_MEMBERS"

  "${RC[@]}" lsf "$remote/uploads/blobs/sha256" --recursive --files-only \
    --include-from "$base/blob-list.txt" --format sp --separator '|' | \
    sort -t '|' -k2,2 > "$base/remote-blob-sizes.txt"
  cmp -s "$base/blob-sizes.txt" "$base/remote-blob-sizes.txt" \
    || die 'remote upload blobs differ before large fetch'
  if ((upload_cipher_bytes > 0)); then
    "${RC[@]}" copy "$remote/uploads/blobs/sha256" "$base/upload-cipher" \
      --include-from "$base/blob-list.txt" --checksum \
      --max-transfer "$upload_transfer_cap" --cutoff-mode hard --transfers 1
  fi
  "$HELPER" verify-cipher-tree --manifest "$manifest_json" --root "$base/upload-cipher"
  while IFS=$'\t' read -r digest size relative; do
    cipher=$base/upload-cipher/${digest:0:2}/${digest}.enc
    restored=$base/uploads/$relative
    mkdir -p -- "$(dirname "$restored")"
    (
      ulimit -f $(((size + 1023) / 1024))
      timeout --signal=TERM --kill-after=10s 1800s \
        openssl enc -d -aes-256-cbc -pbkdf2 -in "$cipher" -out "$restored" \
          -pass file:"$BACKUP_KEY" 2>/dev/null
    ) || die 'upload blob failed bounded decryption'
    chmod 644 "$restored"
  done < <("$HELPER" entries --manifest "$manifest_json")
  find "$base/uploads" -type d -exec chmod 755 {} +
  "$HELPER" verify-restored --manifest "$manifest_json" --root "$base/uploads"

  for service in "${IMAGE_SERVICES[@]}"; do
    image_id=$("$HELPER" image-get --receipt "$artifacts" --service "$service" --key image_id)
    configured_ref=$("$HELPER" image-get --receipt "$artifacts" --service "$service" --key configured_ref)
    image_key=$("$HELPER" image-get --receipt "$artifacts" --service "$service" --key archive_key)
    image_sha=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_cipher_sha256)
    image_bytes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_cipher_bytes)
    image_uncompressed_bytes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_uncompressed_bytes)
    image_uncompressed_inodes=$("$HELPER" image-get --receipt "$artifacts" --service "$service" \
      --key archive_uncompressed_inodes)
    [[ "$image_key" == "images/sha256/${image_id#sha256:}.docker.tar.gz.enc" ]] \
      || die 'Docker archive key is not content addressed by image ID'
    image_cipher=$base/image-cipher/$service.docker.tar.gz.enc
    image_plain=$base/images/$service.docker.tar.gz
    image_expanded_stats=$base/images/$service.uncompressed-bytes.txt
    image_inode_stats=$base/images/$service.uncompressed-inodes.txt
    fetch_checked "$remote/$image_key" "$image_cipher" "$image_sha" "$image_bytes"
    decrypt_file "$image_cipher" "$image_plain"
    "$HELPER" verify-image-archive --archive "$image_plain" --image-id "$image_id" \
      --uncompressed-bytes-output "$image_expanded_stats" \
      --uncompressed-inodes-output "$image_inode_stats"
    [[ "$(tr -d '[:space:]' < "$image_expanded_stats")" == "$image_uncompressed_bytes" ]] \
      || die "expanded Docker archive size differs: $service"
    [[ "$(tr -d '[:space:]' < "$image_inode_stats")" == "$image_uncompressed_inodes" ]] \
      || die "expanded Docker archive inode count differs: $service"
    current_image_id=$(docker image inspect --format '{{.Id}}' "$configured_ref" 2>/dev/null || true)
    [[ -z "$current_image_id" || "$current_image_id" == "$image_id" ]] \
      || die "Docker load would move an existing configured tag: $service"
    docker image load --input "$image_plain" >/dev/null 2>&1 \
      || die "Docker archive cannot be loaded: $service"
    [[ "$(docker image inspect --format '{{.Id}}' "$image_id")" == "$image_id" ]] \
      || die "loaded Docker archive has another image ID: $service"
    image_ids[$service]=$image_id
    configured_refs[$service]=$configured_ref
  done

  status=$("$HELPER" operator-state-get --receipt "$operator" --name policy --key status)
  if [[ "$status" == present ]]; then
    operator_present=1
    for state_name in seasonal_releases seasonal_anchor_replacement policy; do
      archive_name=$("$HELPER" operator-state-get --receipt "$operator" --name "$state_name" \
        --key archive_filename)
      archive_sha=$("$HELPER" operator-state-get --receipt "$operator" --name "$state_name" \
        --key archive_cipher_sha256)
      case "$state_name" in
        seasonal_releases) archive_plain=$base/plain/seasonal-releases.tar.gz ;;
        seasonal_anchor_replacement) archive_plain=$base/plain/seasonal-anchor-replacement.tar.gz ;;
        policy) archive_plain=$base/plain/seasonal-policy.json ;;
      esac
      archive_cipher=$base/cipher/$archive_name
      fetch_bounded "$set_root/$archive_name" "$archive_cipher" "$MAX_OPERATOR_CIPHER_BYTES"
      [[ "$(sha256sum "$archive_cipher" | cut -d' ' -f1)" == "$archive_sha" ]] \
        || die 'operator-state archive ciphertext differs'
      decrypt_file "$archive_cipher" "$archive_plain"
    done
    "$HELPER" verify-tree-archive --archive "$base/plain/seasonal-releases.tar.gz" \
      --prefix seasonal-releases --max-bytes "$MAX_OPERATOR_ARCHIVE_BYTES" \
      --max-members "$MAX_SEASONAL_TREE_MEMBERS"
    "$HELPER" verify-tree-archive --archive "$base/plain/seasonal-anchor-replacement.tar.gz" \
      --prefix seasonal-anchor-replacement --max-bytes "$MAX_OPERATOR_ARCHIVE_BYTES" \
      --max-members "$MAX_SEASONAL_TREE_MEMBERS"
  elif [[ "$status" != absent ]]; then
    die 'operator-state policy status is invalid'
  fi
  [[ "$("$HELPER" operator-state-get --receipt "$operator" --name seasonal_releases --key status)" == "$status" && \
     "$("$HELPER" operator-state-get --receipt "$operator" --name seasonal_anchor_replacement --key status)" == "$status" ]] \
    || die 'operator-state roots/policy status differs'

  offline_args=(
    docker run --rm --name "$offline_container"
    --label "freio.postiz.restore-run=$run_id"
    --label "freio.postiz.restore-role=$offline_role"
    "${PARSER_LIMITS[@]}"
    --network none --read-only --cap-drop ALL
    --security-opt no-new-privileges
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m
    -e "EXPECTED_FILE_COUNT=$upload_count"
    -e "EXPECTED_TOTAL_BYTES=$upload_bytes"
    -e "OPERATOR_STATE_PRESENT=$operator_present"
    -v "$OFFLINE_VERIFY:/drill/verify.sh:ro"
    -v "$base/plain/runtime-config.tar.gz:/drill/runtime-config.tar.gz:ro"
    -v "$base/plain/config-volume.tar.gz:/drill/config-volume.tar.gz:ro"
    -v "$base/uploads:/drill/uploads:ro"
    -v "$base/checksums.txt:/drill/checksums.txt:ro"
    -v "$base/offline-output:/drill-output:rw"
  )
  if ((operator_present)); then
    offline_args+=(
      -v "$base/plain/seasonal-policy.json:/drill/seasonal-policy.json:ro"
      -v "$base/plain/seasonal-releases.tar.gz:/drill/seasonal-releases.tar.gz:ro"
      -v "$base/plain/seasonal-anchor-replacement.tar.gz:/drill/seasonal-anchor-replacement.tar.gz:ro"
    )
  fi
  offline_args+=(--entrypoint /bin/sh "${image_ids[postiz-postgres]}" /drill/verify.sh)
  "${offline_args[@]}"
  "$HELPER" verify-tree-restored --archive "$base/plain/config-volume.tar.gz" \
    --prefix postiz-config --root "$base/offline-output/config-volume/postiz-config" \
    --max-bytes $((64 * 1024 * 1024)) --max-members "$MAX_CONFIG_TREE_MEMBERS"
  if ((operator_present)); then
    "$HELPER" verify-tree-restored --archive "$base/plain/seasonal-releases.tar.gz" \
      --prefix seasonal-releases \
      --root "$base/offline-output/seasonal-releases/seasonal-releases" \
      --max-bytes "$MAX_OPERATOR_ARCHIVE_BYTES" \
      --max-members "$MAX_SEASONAL_TREE_MEMBERS"
    "$HELPER" verify-tree-restored --archive "$base/plain/seasonal-anchor-replacement.tar.gz" \
      --prefix seasonal-anchor-replacement \
      --root "$base/offline-output/seasonal-anchor-replacement/seasonal-anchor-replacement" \
      --max-bytes "$MAX_OPERATOR_ARCHIVE_BYTES" \
      --max-members "$MAX_SEASONAL_TREE_MEMBERS"
    "$HELPER" verify-seasonal-policy \
      --policy "$base/offline-output/seasonal-backup-policy.json" \
      --seasonal-releases-root "$base/offline-output/seasonal-releases/seasonal-releases" \
      --seasonal-anchor-replacement-root \
        "$base/offline-output/seasonal-anchor-replacement/seasonal-anchor-replacement"
  fi

  compose_images=$base/compose-images.txt
  receipt_images=$base/receipt-images.txt
  recovery_override=$base/recovery-image-override.yml
  recovery_ids=$base/recovery-image-ids.txt
  docker compose \
    --env-file "$base/offline-output/runtime/srv/postiz/postiz.env" \
    -f "$base/offline-output/runtime/srv/postiz/docker-compose.yml" \
    config --no-env-resolution --images 2>/dev/null | sort -u > "$compose_images"
  for service in "${IMAGE_SERVICES[@]}"; do
    printf '%s\n' "${configured_refs[$service]}"
  done | sort -u > "$receipt_images"
  cmp -s "$compose_images" "$receipt_images" \
    || die 'Compose service image references differ from the four-image receipt'

  {
    printf 'services:\n'
    for service in "${IMAGE_SERVICES[@]}"; do
      printf '  %s:\n    image: "%s"\n    pull_policy: never\n' \
        "$service" "${image_ids[$service]}"
    done
  } > "$recovery_override"
  chmod 600 "$recovery_override"
  docker compose \
    --env-file "$base/offline-output/runtime/srv/postiz/postiz.env" \
    -f "$base/offline-output/runtime/srv/postiz/docker-compose.yml" \
    -f "$recovery_override" config --no-env-resolution --images 2>/dev/null | sort -u > "$recovery_ids"
  for service in "${IMAGE_SERVICES[@]}"; do
    printf '%s\n' "${image_ids[$service]}"
  done | sort -u > "$base/receipt-image-ids.txt"
  cmp -s "$recovery_ids" "$base/receipt-image-ids.txt" \
    || die 'fresh-host Compose override is not pinned to the four loaded image IDs'

  restore_logical_databases "$base" "$label" "${image_ids[postiz-postgres]}" "$evidence"
  restore_physical_cluster "$base" "$label" "${image_ids[postiz-postgres]}" "$evidence"
  restore_redis "$base" "$label" "${image_ids[postiz-redis]}" "$evidence"
  log "$label full recovery graph OK ($upload_count uploads, four Docker images)"
  rm -rf -- "$base"
}

restore_one_remote primary "$PRIMARY_ROOT"
restore_one_remote dr "$DR_ROOT"
log "Postiz recovery set $set_timestamp restored independently from primary and DR"
