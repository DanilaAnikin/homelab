#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

readonly DOCKER_BIN=/usr/bin/docker
readonly FLOCK_BIN=/usr/bin/flock
readonly SLEEP_BIN=/usr/bin/sleep
readonly MKTEMP_BIN=/usr/bin/mktemp
readonly CHMOD_BIN=/usr/bin/chmod
readonly MV_BIN=/usr/bin/mv
readonly CONTAINER=supabase-db
readonly DATABASE=freio
readonly DATABASE_ADMIN=postgres
readonly BATCH_SIZE=1000
readonly MAX_BATCHES=60
readonly MAX_NO_PROGRESS=5
readonly STATE_DIR=${STATE_DIRECTORY:-/var/lib/freio-analytics-retention}
readonly DOCKER_CONFIG_DIR=${STATE_DIR}/docker-config
readonly LOCK_FILE=${STATE_DIR}/run.lock
readonly SUCCESS_FILE=${STATE_DIR}/last-success.json

fail() {
  /usr/bin/printf '{"ok":false,"component":"freio-analytics-retention","error":"%s"}\n' "$1" >&2
  exit 1
}

[[ "$(/usr/bin/id -u)" == 0 ]] || fail root_required
[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fail state_directory_invalid
[[ ! -e "$DOCKER_CONFIG_DIR" || ( -d "$DOCKER_CONFIG_DIR" && ! -L "$DOCKER_CONFIG_DIR" ) ]] \
  || fail docker_config_directory_invalid
/usr/bin/install -d -m 0700 "$DOCKER_CONFIG_DIR"
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

exec 9>"$LOCK_FILE"
"$FLOCK_BIN" -n 9 || fail already_running

running=$(
  "$DOCKER_BIN" inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null
) || fail database_container_missing
[[ "$running" == true ]] || fail database_container_not_running

purge_first_batch() {
  "$DOCKER_BIN" exec -i "$CONTAINER" \
    psql -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
    -v "batch_size=$BATCH_SIZE" \
    -U "$DATABASE_ADMIN" -d "$DATABASE" <<'SQL'
SET statement_timeout = '10s';
SET ROLE service_role;
SELECT deleted, has_more, cutoff
FROM public.purge_analytics_events_retention(:batch_size, NULL::TIMESTAMPTZ);
SQL
}

purge_next_batch() {
  local fixed_cutoff=$1
  "$DOCKER_BIN" exec -i "$CONTAINER" \
    psql -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
    -v "batch_size=$BATCH_SIZE" \
    -v "retention_cutoff=$fixed_cutoff" \
    -U "$DATABASE_ADMIN" -d "$DATABASE" <<'SQL'
SET statement_timeout = '10s';
SET ROLE service_role;
SELECT deleted, has_more, cutoff
FROM public.purge_analytics_events_retention(
  :batch_size,
  :'retention_cutoff'::TIMESTAMPTZ
);
SQL
}

fixed_cutoff=""
total_deleted=0
batch_count=0
no_progress=0

while (( batch_count < MAX_BATCHES )); do
  if [[ -z "$fixed_cutoff" ]]; then
    result=$(purge_first_batch) || fail purge_rpc_failed
  else
    result=$(purge_next_batch "$fixed_cutoff") || fail purge_rpc_failed
  fi

  [[ "$result" != *$'\n'* ]] || fail purge_rpc_shape_invalid
  IFS='|' read -r deleted has_more returned_cutoff extra <<<"$result"
  [[ -z "${extra:-}" ]] || fail purge_rpc_shape_invalid
  [[ "$deleted" =~ ^[0-9]+$ ]] || fail purge_rpc_deleted_invalid
  [[ "$has_more" == t || "$has_more" == f ]] || fail purge_rpc_has_more_invalid
  [[ "$returned_cutoff" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9:.]+[+-][0-9:]+$ ]] \
    || fail purge_rpc_cutoff_invalid

  if [[ -z "$fixed_cutoff" ]]; then
    fixed_cutoff=$returned_cutoff
  elif [[ "$returned_cutoff" != "$fixed_cutoff" ]]; then
    fail purge_rpc_cutoff_changed
  fi

  (( batch_count += 1 ))
  total_deleted=$(( total_deleted + deleted ))

  if [[ "$has_more" == f ]]; then
    temporary=$("$MKTEMP_BIN" "${STATE_DIR}/.last-success.XXXXXX")
    "$CHMOD_BIN" 0600 "$temporary"
    /usr/bin/printf \
      '{"ok":true,"component":"freio-analytics-retention","cutoff":"%s","deleted":%d,"batches":%d}\n' \
      "$fixed_cutoff" "$total_deleted" "$batch_count" >"$temporary"
    "$MV_BIN" -fT "$temporary" "$SUCCESS_FILE"
    /usr/bin/printf \
      '{"ok":true,"component":"freio-analytics-retention","cutoff":"%s","deleted":%d,"batches":%d,"has_more":false}\n' \
      "$fixed_cutoff" "$total_deleted" "$batch_count"
    exit 0
  fi

  if (( deleted == 0 )); then
    (( no_progress += 1 ))
    if (( no_progress >= MAX_NO_PROGRESS )); then
      fail locked_or_nonprogressing_backlog
    fi
    "$SLEEP_BIN" 1
  else
    no_progress=0
  fi
done

fail safety_cap_backlog_remaining
