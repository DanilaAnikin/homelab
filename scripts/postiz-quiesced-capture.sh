#!/usr/bin/env bash
# Create one bounded, crash-recoverable, writer-fenced Postiz source snapshot.
# This script never performs an off-site mutation.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py
readonly RUN_ROOT=/run/homelab-backup
readonly MUTATION_LOCK=$RUN_ROOT/postiz-mutation.lock
readonly SEASONAL_LAUNCH_LOCK=$RUN_ROOT/freio-seasonal-anchors.launch.lock
readonly SEASONAL_ENGINE_LOCK=$RUN_ROOT/freio-seasonal-anchors.engine.lock
readonly STATE_ROOT=/var/lib/homelab-backup
readonly JOURNAL=$STATE_ROOT/postiz-quiesce-journal.json
readonly POSTGRES_NAME=postiz-postgres
readonly POSTGRES_FENCED_NAME=postiz-postgres-backup-fenced
readonly INTERNAL_NETWORK=postiz_postiz-internal
readonly CONFIG_VOLUME=/var/lib/docker/volumes/postiz_postiz-config/_data
readonly UPLOAD_VOLUME=/var/lib/docker/volumes/postiz_postiz-uploads/_data
readonly REDIS_VOLUME=/var/lib/docker/volumes/postiz_postiz-redis/_data
readonly SEASONAL_POLICY=/var/lib/freio-content/seasonal-backup-policy.json
readonly MAX_CAPTURE_SECONDS=300
readonly MAX_RECOVERY_SECONDS=360
readonly MIN_FREE_BYTES=$((48 * 1024 * 1024 * 1024))
readonly MIN_FREE_INODES=250000
readonly MAX_CAPTURE_WORKSPACE_BYTES=$((96 * 1024 * 1024 * 1024))
readonly MAX_PG_SOURCE_BYTES=$((24 * 1024 * 1024 * 1024))
readonly MAX_PG_SOURCE_INODES=1000000
readonly MAX_UPLOAD_SOURCE_BYTES=$((16 * 1024 * 1024 * 1024))
readonly MAX_UPLOAD_SOURCE_INODES=100000
readonly MAX_CONFIG_SOURCE_BYTES=$((48 * 1024 * 1024))
readonly MAX_CONFIG_TREE_MEMBERS=4096
readonly MAX_REDIS_SOURCE_BYTES=$((2 * 1024 * 1024 * 1024 - 1024 * 1024))
readonly MAX_SEASONAL_SOURCE_BYTES=$((480 * 1024 * 1024))
readonly MAX_SEASONAL_TREE_MEMBERS=10000
readonly MAX_GLOBALS_BYTES=$((64 * 1024 * 1024 - 1024 * 1024))
readonly MAX_LOGICAL_DUMP_BYTES=$((4 * 1024 * 1024 * 1024 - 1024 * 1024))
readonly MAX_PHYSICAL_ARCHIVE_BYTES=$((6 * 1024 * 1024 * 1024 - 1024 * 1024))
readonly MAX_RUNTIME_CONFIG_MEMBER_BYTES=$((16 * 1024 * 1024))
readonly MAX_RUNTIME_CONFIG_EXPANDED_BYTES=$((64 * 1024 * 1024))
readonly MAX_RUNTIME_ARCHIVE_BYTES=$((72 * 1024 * 1024))
readonly -a DATABASES=(postiz temporal temporal_visibility insights)
readonly -a STOP_ORDER=(postiz postiz-temporal postiz-redis)
readonly -a START_ORDER=(postiz-postgres postiz-redis postiz-temporal postiz)
readonly -a RUNTIME_CONFIG_SOURCES=(
  etc/homelab/postiz-backup-source-revision
  etc/systemd/system/backup.service
  etc/systemd/system/backup.timer
  etc/systemd/system/frequent-db-backup.service
  etc/systemd/system/frequent-db-backup.timer
  etc/systemd/system/postiz-backup-workspace-cleanup.service
  etc/systemd/system/postiz-quiesce-recover.service
  etc/systemd/system/postiz-restore-cleanup.service
  etc/systemd/system/restore-drill.service
  etc/systemd/system/restore-drill.timer
  etc/tmpfiles.d/homelab-backup.conf
  srv/postiz/postiz.env
  srv/postiz/docker-compose.yml
  srv/postiz/Dockerfile.patch
  srv/postiz/schedule-week.py
  srv/homelab/self-healing/postiz-offline-verify.sh
  srv/homelab/self-healing/postiz-restore-drill.sh
  srv/homelab/self-healing/restore-drill.sh
  usr/local/bin/frequent-db-backup.sh
  usr/local/bin/homelab-backup.sh
  usr/local/libexec/postiz-backup-manifest.py
  usr/local/sbin/postiz-artifact-backup.sh
  usr/local/sbin/postiz-backup-workspace-cleanup.sh
  usr/local/sbin/postiz-compose-locked.sh
  usr/local/sbin/postiz-quiesced-capture.sh
  usr/local/sbin/postiz-r2-policy-attest.sh
)

die() { printf 'Postiz quiesced capture: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'usage: %s --timestamp YYYYMMDDTHHMMSSZ --output-dir /var/lib/homelab-backup/... | --recover-only\n' "$0" >&2
  exit 64
}

safe_fixed_path() {
  local path=$1 type=$2 mode=$3 actual
  if [[ "$type" == file ]]; then
    [[ -f "$path" && ! -L "$path" ]] || die "required file is missing or unsafe: $path"
    actual=$(stat -Lc '%u:%g:%a:%h' -- "$path")
    [[ "$actual" == "0:0:${mode}:1" ]] || die "owner/mode/link contract failed: $path"
  else
    [[ -d "$path" && ! -L "$path" ]] || die "required directory is missing or unsafe: $path"
    actual=$(stat -Lc '%u:%g:%a' -- "$path")
    [[ "$actual" == "0:0:${mode}" ]] || die "owner/mode contract failed: $path"
  fi
}

timestamp=
output_dir=
recover_only=0
supervised_worker=0
while (($#)); do
  case "$1" in
    --timestamp) (($# >= 2)) || usage; timestamp=$2; shift 2 ;;
    --output-dir) (($# >= 2)) || usage; output_dir=$2; shift 2 ;;
    --recover-only) recover_only=1; shift ;;
    --supervised-worker) supervised_worker=1; shift ;;
    *) usage ;;
  esac
done
if ((recover_only)); then
  [[ -z "$timestamp$output_dir" && "$supervised_worker" -eq 0 ]] || usage
else
  [[ "$timestamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || usage
  [[ "$output_dir" == "$STATE_ROOT/"* && "$output_dir" != */../* && "$output_dir" != */./* ]] || usage
fi
((EUID == 0)) || die 'must run as root'
safe_fixed_path "$HELPER" file 755
safe_fixed_path "$RUN_ROOT" directory 700
safe_fixed_path "$MUTATION_LOCK" file 600
safe_fixed_path "$SEASONAL_LAUNCH_LOCK" file 600
safe_fixed_path "$SEASONAL_ENGINE_LOCK" file 600
safe_fixed_path "$STATE_ROOT" directory 700

# Bash defers a TERM trap while it waits for some foreground commands.  The
# outer supervisor therefore enforces the wall clock outside the capture
# process, escalates to KILL at 300 s, and immediately runs durable recovery.
if ((!recover_only && !supervised_worker)); then
  pre_recovery_rc=0
  timeout --signal=TERM --kill-after=15s 480s "$0" --recover-only || pre_recovery_rc=$?
  worker_rc=1
  if ((pre_recovery_rc == 0)); then
    worker_rc=0
    timeout --signal=TERM --kill-after=5s 295s "$0" \
      --timestamp "$timestamp" --output-dir "$output_dir" --supervised-worker \
      || worker_rc=$?
  fi
  final_recovery_rc=0
  if ((worker_rc != 0)); then
    timeout --signal=TERM --kill-after=15s 480s "$0" --recover-only || final_recovery_rc=$?
  fi
  ((pre_recovery_rc == 0 && worker_rc == 0 && final_recovery_rc == 0)) \
    || die 'supervised capture or mandatory writer recovery failed'
  exit 0
fi

# A no-journal recovery probe has nothing to serialize.  This prevents a
# successful backup's ExecStopPost from racing a legitimate frequent dump for
# the mutation lock.  Any present path (including a symlink) takes the strict
# locked validation/recovery path below.
if ((recover_only)) && [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]; then
  exit 0
fi

exec 8<>"$MUTATION_LOCK"
lock_path_metadata=$(stat -Lc '%u:%g:%a:%h:%d:%i' -- "$MUTATION_LOCK")
lock_fd_metadata=$(stat -Lc '%u:%g:%a:%h:%d:%i' -- "/proc/$$/fd/8")
[[ "$lock_path_metadata" == "$lock_fd_metadata" ]] || die 'mutation lock descriptor/path drifted'
flock -w 60 8 || die 'another Postiz mutation is in progress'

container_ready() {
  local service=$1 id=$2
  case "$service" in
    postiz-postgres)
      docker exec "$id" pg_isready -U postiz >/dev/null 2>&1
      ;;
    postiz-redis)
      [[ "$(docker exec "$id" redis-cli ping 2>/dev/null)" == PONG ]]
      ;;
    postiz-temporal)
      docker exec "$id" sh -ec '
        test ! -e /nonexistent
        command -v temporal >/dev/null 2>&1
        exec env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent \
          temporal operator cluster health \
            --address postiz-temporal:7233 \
            --namespace default \
            --env-file /dev/null \
            --color never \
            --log-level never \
            --output json >/dev/null 2>&1
      '
      ;;
    postiz)
      docker exec "$id" node -e '
        const net=require("net");
        const s=net.connect(5000,"127.0.0.1");
        const t=setTimeout(()=>process.exit(1),1500);
        s.on("connect",()=>{clearTimeout(t);s.destroy();process.exit(0)});
        s.on("error",()=>process.exit(1));
      ' >/dev/null 2>&1
      ;;
    *) return 1 ;;
  esac
}

wait_ready() {
  local service=$1 id=$2 deadline=$3
  while ((SECONDS < deadline)); do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null)" == true ]] && \
       container_ready "$service" "$id"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

reap_capture_parser() {
  local capture_timestamp parser rows listed_id listed_name listed_extra inspection
  capture_timestamp=$("$HELPER" journal-get --journal "$JOURNAL" --key created_at) \
    || return 1
  parser=postiz-capture-redis-check-$capture_timestamp
  rows=$(timeout --signal=TERM --kill-after=5s 30s docker ps -a --no-trunc \
    --filter "name=^/${parser}$" --format '{{.ID}}|{{.Names}}' 2>/dev/null) \
    || return 1
  [[ "$rows" != *$'\n'* ]] || return 1
  [[ -n "$rows" ]] || return 0
  IFS='|' read -r listed_id listed_name listed_extra <<< "$rows"
  [[ "$listed_id" =~ ^[0-9a-f]{64}$ && "$listed_name" == "$parser" && \
     -z "$listed_extra" ]] || return 1
  inspection=$(timeout --signal=TERM --kill-after=5s 30s docker inspect --format \
    '{{.Name}}|{{with .Config.Labels}}{{index . "freio.postiz.capture-run"}}{{end}}|{{with .Config.Labels}}{{index . "freio.postiz.capture-role"}}{{end}}' \
    "$listed_id" 2>/dev/null) || return 1
  [[ "$inspection" == "/$parser|$capture_timestamp|redis-check" ]] || return 1
  timeout --signal=TERM --kill-after=10s 30s docker rm -f "$listed_id" >/dev/null \
    || return 1
}

recover_from_journal() {
  [[ -e "$JOURNAL" || -L "$JOURNAL" ]] || return 0
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" && "$(stat -Lc '%u:%g:%a:%h' "$JOURNAL")" == 0:0:600:1 ]] \
    || { printf 'Postiz recovery: unsafe quiesce journal\n' >&2; return 1; }
  "$HELPER" update-quiesce-journal --journal "$JOURNAL" --phase restoring || return 1
  local parser_recovery_rc=0
  reap_capture_parser || {
    printf 'Postiz recovery: capture parser cleanup failed\n' >&2
    parser_recovery_rc=1
  }
  local service id image actual actual_id actual_image actual_name remaining start_timeout
  local recovery_deadline=$((SECONDS + MAX_RECOVERY_SECONDS))
  for service in "${START_ORDER[@]}"; do
    id=$("$HELPER" journal-get --journal "$JOURNAL" --service "$service" --key container_id) || return 1
    image=$("$HELPER" journal-get --journal "$JOURNAL" --service "$service" --key image_id) || return 1
    actual=$(docker inspect --format '{{.Id}}|{{.Image}}|{{.Name}}' "$id" 2>/dev/null) || return 1
    IFS='|' read -r actual_id actual_image actual_name <<< "$actual"
    [[ "$actual_id|$actual_image" == "$id|$image" ]] || {
      printf 'Postiz recovery: exact container identity drifted: %s\n' "$service" >&2
      return 1
    }
    if [[ "$service" == "$POSTGRES_NAME" ]]; then
      case "$actual_name" in
        "/$POSTGRES_NAME") ;;
        "/$POSTGRES_FENCED_NAME") docker rename "$id" "$POSTGRES_NAME" || return 1 ;;
        *) printf 'Postiz recovery: unexpected Postgres container name\n' >&2; return 1 ;;
      esac
    elif [[ "$actual_name" != "/$service" ]]; then
      printf 'Postiz recovery: exact container name drifted: %s\n' "$service" >&2
      return 1
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' "$id")" != true ]]; then
      remaining=$((recovery_deadline - SECONDS))
      ((remaining > 0)) || return 1
      start_timeout=$remaining
      ((start_timeout > 60)) && start_timeout=60
      timeout --signal=TERM --kill-after=10s "${start_timeout}s" \
        docker start "$id" >/dev/null || return 1
    fi
    wait_ready "$service" "$id" "$recovery_deadline" || {
      printf 'Postiz recovery: readiness failed: %s\n' "$service" >&2
      return 1
    }
  done
  ((parser_recovery_rc == 0)) || return 1
  rm -f -- "$JOURNAL"
  sync -f "$STATE_ROOT"
}

recover_from_journal || die 'stale writer-fence recovery failed'
((recover_only)) && exit 0
exec 6<>"$SEASONAL_LAUNCH_LOCK"
exec 7<>"$SEASONAL_ENGINE_LOCK"
for lock_fd in 6 7; do
  lock_path=$SEASONAL_LAUNCH_LOCK
  [[ "$lock_fd" == 7 ]] && lock_path=$SEASONAL_ENGINE_LOCK
  [[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$lock_path")" == \
     "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/$lock_fd")" ]] \
    || die 'seasonal lock descriptor/path drifted'
  flock -n "$lock_fd" || die 'seasonal producer is active; capture deferred before writer stop'
done
[[ ! -e "$output_dir" && ! -L "$output_dir" ]] || die 'output directory must not exist'

free_bytes=$(df -B1 --output=avail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
free_inodes=$(df --output=iavail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
[[ "$free_bytes" =~ ^[0-9]+$ && "$free_inodes" =~ ^[0-9]+$ ]] || die 'cannot measure capture workspace capacity'
((free_bytes >= MIN_FREE_BYTES && free_inodes >= MIN_FREE_INODES)) \
  || die 'capture workspace byte/inode preflight failed'

mkdir -m 700 "$output_dir"

for volume_spec in \
  "postiz_postiz-config|$CONFIG_VOLUME" \
  "postiz_postiz-uploads|$UPLOAD_VOLUME" \
  "postiz_postiz-redis|$REDIS_VOLUME"; do
  IFS='|' read -r volume expected <<< "$volume_spec"
  [[ "$(docker volume inspect -f '{{.Mountpoint}}' "$volume")" == "$expected" ]] \
    || die "volume mountpoint drifted: $volume"
done

actual_mounts=$output_dir/mounts.txt
install -m 600 /dev/null "$actual_mounts"
for service in postiz postiz-postgres postiz-redis postiz-temporal; do
  docker inspect --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}{{println}}{{end}}' \
    "$service" | while IFS='|' read -r mount_type mount_name destination; do
      [[ -z "$mount_type$mount_name$destination" ]] && continue
      printf '%s|%s|%s|%s\n' "$service" "$mount_type" "$mount_name" "$destination"
    done >> "$actual_mounts"
done
sort -o "$actual_mounts" "$actual_mounts"
expected_mounts='postiz-postgres|volume|postiz_postiz-postgres|/var/lib/postgresql/data
postiz-redis|volume|postiz_postiz-redis|/data
postiz|volume|postiz_postiz-config|/config
postiz|volume|postiz_postiz-uploads|/uploads'
[[ "$(cat "$actual_mounts")" == "$expected_mounts" ]] || die 'persistent mount coverage drifted'

network_members=$(docker network inspect "$INTERNAL_NETWORK" --format \
  '{{range $id, $container := .Containers}}{{$container.Name}}{{println}}{{end}}' | sort)
expected_network_members='postiz
postiz-postgres
postiz-redis
postiz-temporal'
[[ "$network_members" == "$expected_network_members" ]] \
  || die 'Postiz internal-network writer topology drifted'

database_identity=$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}' postiz-postgres)
IFS='|' read -r PG_CONTAINER_ID PG_IMAGE_ID pg_running pg_paused <<< "$database_identity"
[[ "$PG_CONTAINER_ID" =~ ^[0-9a-f]{64}$ && "$PG_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ && \
   "$pg_running|$pg_paused" == true\|false ]] || die 'Postgres dependency is not exact running/unpaused'

pg_scalar() {
  local database=$1 query=$2 result
  result=$(timeout --signal=TERM --kill-after=5s 20s docker exec \
    -e PGAPPNAME=freio-postiz-backup \
    -e PGOPTIONS='-c statement_timeout=15000 -c lock_timeout=3000 -c TimeZone=UTC -c DateStyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3' \
    "$PG_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 -U postiz -d "$database" -Atc "$query" 2>/dev/null) \
    || die "Postgres evidence query failed: $database"
  result=${result//[[:space:]]/}
  [[ "$result" =~ ^[0-9]+$ ]] || die "Postgres evidence is not numeric: $database"
  printf '%s\n' "$result"
}

pg_fingerprint() {
  local database=$1 query=$2 digest
  digest=$(timeout --signal=TERM --kill-after=5s 30s docker exec \
    -e PGAPPNAME=freio-postiz-backup \
    -e PGOPTIONS='-c statement_timeout=25000 -c lock_timeout=3000 -c TimeZone=UTC -c DateStyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3' \
    "$PG_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 -U postiz -d "$database" -Atc "$query" \
    2>/dev/null | sha256sum | cut -d' ' -f1) \
    || die "Postgres fingerprint query failed: $database"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "Postgres fingerprint is invalid: $database"
  printf '%s\n' "$digest"
}

database_inventory=$(timeout 20s docker exec -e PGAPPNAME=freio-postiz-backup \
  "$PG_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 \
  -U postiz -d postgres -Atc "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1" 2>/dev/null)
expected_inventory='insights
postgres
postiz
temporal
temporal_visibility'
[[ "$database_inventory" == "$expected_inventory" ]] || die 'non-template Postgres database inventory drifted'
postgres_user_objects=$(pg_scalar postgres "
  SELECT
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')
    +
    (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')")
[[ "$postgres_user_objects" == 0 ]] || die 'maintenance postgres database now contains user objects'
[[ "$(pg_scalar postgres "SELECT count(*) FROM pg_roles WHERE rolname='freio_restore_bootstrap'")" == 0 ]] \
  || die 'reserved restore-bootstrap role unexpectedly exists in production'
long_transactions=$(pg_scalar postgres "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND backend_type='client backend' AND xact_start < now()-interval '30 seconds'")
lock_waiters=$(pg_scalar postgres "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND wait_event_type='Lock'")
[[ "$long_transactions" == 0 && "$lock_waiters" == 0 ]] || die 'unsafe long transaction or lock waiter exists before fence'

declare -A container_ids=()
declare -A image_ids=()
journal_args=()
for service in postiz-postgres "${STOP_ORDER[@]}"; do
  state=$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}' "$service")
  IFS='|' read -r id image running paused <<< "$state"
  [[ "$id" =~ ^[0-9a-f]{64}$ && "$image" =~ ^sha256:[0-9a-f]{64}$ && "$running|$paused" == true\|false ]] \
    || die "writer is not exact running/unpaused: $service"
  if [[ "$service" == postiz-postgres && "$id|$image" != "$PG_CONTAINER_ID|$PG_IMAGE_ID" ]]; then
    die 'Postgres dependency identity changed before journal commit'
  fi
  container_ids[$service]=$id
  image_ids[$service]=$image
  journal_args+=(--container "$service|$id|$image")
done

assert_network_contract() {
  local service id networks expected_networks aliases port_bindings
  for service in postiz postiz-postgres postiz-redis postiz-temporal; do
    id=${container_ids[$service]}
    networks=$(docker inspect --format \
      '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{println}}{{end}}' "$id" | sort)
    if [[ "$service" == postiz ]]; then
      expected_networks='dokploy-network
postiz_postiz-internal'
    else
      expected_networks='postiz_postiz-internal'
    fi
    [[ "$networks" == "$expected_networks" ]] \
      || die "service network set drifted: $service"
    while IFS= read -r network; do
      aliases=$(docker inspect --format \
        "{{with index .NetworkSettings.Networks \"$network\"}}{{range .Aliases}}{{.}}{{println}}{{end}}{{end}}" \
        "$id" | sort -u)
      [[ "$aliases" == "$service" ]] || die "service network aliases drifted: $service"
    done <<< "$networks"
    port_bindings=$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$id")
    [[ "$port_bindings" == '{}' || "$port_bindings" == null ]] \
      || die "service unexpectedly has a host-published port: $service"
    [[ -z "$(docker port "$id" 2>/dev/null)" ]] \
      || die "service unexpectedly publishes a host port: $service"
  done
  network_members=$(docker network inspect "$INTERNAL_NETWORK" --format \
    '{{range $id, $container := .Containers}}{{$id}}|{{$container.Name}}{{println}}{{end}}' | sort -t '|' -k2,2)
  expected_network_members=
  for service in postiz postiz-postgres postiz-redis postiz-temporal; do
    expected_network_members+="${container_ids[$service]}|$service"$'\n'
  done
  expected_network_members=${expected_network_members%$'\n'}
  [[ "$network_members" == "$expected_network_members" ]] \
    || die 'Postiz internal-network exact ID membership drifted'
}
assert_network_contract

tree_bytes() {
  local root=$1 value
  value=$(du -sb --one-file-system -- "$root" | awk 'NR == 1 {print $1}')
  [[ "$value" =~ ^[0-9]+$ ]] || die "cannot measure source tree bytes: $root"
  printf '%s\n' "$value"
}

tree_inodes() {
  local root=$1 value
  value=$(find "$root" -xdev -printf '.' | wc -c)
  [[ "$value" =~ ^[0-9]+$ ]] || die "cannot measure source tree inodes: $root"
  printf '%s\n' "$value"
}

pg_source_kib=$(timeout --signal=TERM --kill-after=5s 30s docker exec \
  "${container_ids[postiz-postgres]}" du -sk /var/lib/postgresql/data | awk 'NR == 1 {print $1}')
[[ "$pg_source_kib" =~ ^[0-9]+$ ]] || die 'cannot measure Postgres source size'
pg_source_bytes=$((pg_source_kib * 1024))
[[ "$pg_source_bytes" =~ ^[0-9]+$ && "$pg_source_bytes" -le MAX_PG_SOURCE_BYTES ]] \
  || die 'Postgres source size exceeds capture contract'
pg_source_inodes=$(timeout --signal=TERM --kill-after=5s 30s docker exec \
  "${container_ids[postiz-postgres]}" sh -ec \
  'find /var/lib/postgresql/data -xdev -print | wc -l')
pg_source_inodes=${pg_source_inodes//[[:space:]]/}
[[ "$pg_source_inodes" =~ ^[0-9]+$ && "$pg_source_inodes" -le MAX_PG_SOURCE_INODES ]] \
  || die 'Postgres source inode count exceeds capture contract'
upload_source_bytes=$(tree_bytes "$UPLOAD_VOLUME")
upload_source_inodes=$(tree_inodes "$UPLOAD_VOLUME")
config_source_bytes=$(tree_bytes "$CONFIG_VOLUME")
config_source_inodes=$(tree_inodes "$CONFIG_VOLUME")
redis_source_bytes=$(tree_bytes "$REDIS_VOLUME")
((upload_source_bytes <= MAX_UPLOAD_SOURCE_BYTES && \
  upload_source_inodes <= MAX_UPLOAD_SOURCE_INODES && \
  config_source_bytes <= MAX_CONFIG_SOURCE_BYTES && \
  config_source_inodes <= MAX_CONFIG_TREE_MEMBERS + 1 && \
  redis_source_bytes <= MAX_REDIS_SOURCE_BYTES)) \
  || die 'persistent-volume source byte/inode ceiling exceeded'
seasonal_source_bytes=0
seasonal_source_inodes=0
for seasonal_root in /var/lib/freio-content/seasonal-releases \
    /var/lib/freio-content/seasonal-anchor-replacement; do
  if [[ -e "$seasonal_root" || -L "$seasonal_root" ]]; then
    [[ -d "$seasonal_root" && ! -L "$seasonal_root" ]] \
      || die 'seasonal source root is unsafe'
    seasonal_bytes=$(tree_bytes "$seasonal_root")
    seasonal_inodes=$(tree_inodes "$seasonal_root")
    ((seasonal_bytes <= MAX_SEASONAL_SOURCE_BYTES && \
      seasonal_inodes <= MAX_SEASONAL_TREE_MEMBERS + 1)) \
      || die 'seasonal source exceeds byte/member ceiling'
    seasonal_source_bytes=$((seasonal_source_bytes + seasonal_bytes))
    seasonal_source_inodes=$((seasonal_source_inodes + seasonal_inodes))
  fi
done
required_capture_bytes=$((
  pg_source_bytes * 3 + upload_source_bytes + config_source_bytes * 2
  + redis_source_bytes * 2 + seasonal_source_bytes * 2
  + 4 * 1024 * 1024 * 1024
))
required_capture_inodes=$((
  pg_source_inodes + upload_source_inodes + config_source_inodes
  + seasonal_source_inodes + 10000
))
((required_capture_bytes <= MAX_CAPTURE_WORKSPACE_BYTES)) \
  || die 'declared capture workspace exceeds hard byte ceiling'
free_bytes=$(df -B1 --output=avail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
free_inodes=$(df --output=iavail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
((free_bytes >= required_capture_bytes && free_inodes >= required_capture_inodes)) \
  || die 'capture workspace capacity is insufficient before writer stop'

verify_compose_generation() {
  local suffix=$1
  local resolved_compose=$output_dir/runtime-compose-$suffix.json
  local source_hashes=$output_dir/runtime-compose-$suffix-source-hashes.txt
  local resolved_semantic_hashes=$output_dir/runtime-compose-$suffix-resolved-hashes.txt
  local no_deps_input=$output_dir/runtime-compose-$suffix-no-deps-input.json
  local no_deps_compose=$output_dir/runtime-compose-$suffix-no-deps.json
  local no_deps_hash=$output_dir/runtime-compose-$suffix-no-deps-hash.txt
  local container_runtime=$output_dir/runtime-containers-$suffix.json
  local image_runtime=$output_dir/runtime-images-$suffix.json
  local network_runtime=$output_dir/runtime-networks-$suffix.json
  timeout --signal=TERM --kill-after=5s 30s docker compose \
    --env-file /srv/postiz/postiz.env -f /srv/postiz/docker-compose.yml \
    config --format json > "$resolved_compose"
  timeout --signal=TERM --kill-after=5s 30s docker compose \
    --env-file /srv/postiz/postiz.env -f /srv/postiz/docker-compose.yml \
    config --hash '*' > "$source_hashes"
  timeout --signal=TERM --kill-after=5s 30s docker compose \
    -f "$resolved_compose" config --hash '*' > "$resolved_semantic_hashes"
  "$HELPER" write-compose-no-deps-model --compose-json "$resolved_compose" \
    --output "$no_deps_input"
  timeout --signal=TERM --kill-after=5s 30s docker compose \
    -f "$no_deps_input" config --format json > "$no_deps_compose"
  timeout --signal=TERM --kill-after=5s 30s docker compose \
    -f "$no_deps_input" config --hash postiz > "$no_deps_hash"
  timeout --signal=TERM --kill-after=5s 20s docker inspect \
    "${container_ids[postiz]}" "${container_ids[postiz-postgres]}" \
    "${container_ids[postiz-redis]}" "${container_ids[postiz-temporal]}" > "$container_runtime"
  timeout --signal=TERM --kill-after=5s 20s docker image inspect \
    "${image_ids[postiz]}" "${image_ids[postiz-postgres]}" \
    "${image_ids[postiz-redis]}" "${image_ids[postiz-temporal]}" > "$image_runtime"
  timeout --signal=TERM --kill-after=5s 20s docker network inspect \
    dokploy-network "$INTERNAL_NETWORK" > "$network_runtime"
  chmod 600 "$resolved_compose" "$source_hashes" "$resolved_semantic_hashes" "$no_deps_input" \
    "$no_deps_compose" "$no_deps_hash" "$container_runtime" "$image_runtime" "$network_runtime"
  "$HELPER" verify-compose-runtime --compose-json "$resolved_compose" \
    --compose-hashes "$source_hashes" \
    --resolved-compose-hashes "$resolved_semantic_hashes" \
    --postiz-no-deps-compose-json "$no_deps_compose" \
    --postiz-no-deps-hash "$no_deps_hash" \
    --container-json "$container_runtime" \
    --image-inspect-json "$image_runtime" \
    --network-inspect-json "$network_runtime" \
    --runtime-state "$suffix" \
    --expected-image "postiz|${image_ids[postiz]}" \
    --expected-image "postiz-postgres|${image_ids[postiz-postgres]}" \
    --expected-image "postiz-redis|${image_ids[postiz-redis]}" \
    --expected-image "postiz-temporal|${image_ids[postiz-temporal]}"
  rm -f -- "$resolved_compose" "$source_hashes" "$resolved_semantic_hashes" "$no_deps_input" \
    "$no_deps_compose" "$no_deps_hash" "$container_runtime" "$image_runtime" "$network_runtime"
}

# Fail generation drift before the durable journal and before the first stop.
verify_compose_generation preflight

"$HELPER" write-quiesce-journal --timestamp "$timestamp" --phase prepared \
  "${journal_args[@]}" --output "$JOURNAL"

cleanup() {
  local rc=0
  recover_from_journal || rc=1
  return "$rc"
}
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

capture_started=$(date +%s)

"$HELPER" update-quiesce-journal --journal "$JOURNAL" --phase stopping
timeout --signal=TERM --kill-after=10s 60s docker stop --time 30 "${container_ids[postiz]}" >/dev/null
timeout --signal=TERM --kill-after=10s 60s docker stop --time 30 "${container_ids[postiz-temporal]}" >/dev/null
timeout --signal=TERM --kill-after=5s 20s \
  docker rename "${container_ids[postiz-postgres]}" "$POSTGRES_FENCED_NAME"
[[ "$(docker inspect --format '{{.Name}}' "${container_ids[postiz-postgres]}")" == \
   "/$POSTGRES_FENCED_NAME" ]] || die 'Postgres external docker-exec fence failed'
[[ "$(docker exec "${container_ids[postiz-redis]}" redis-cli SAVE 2>/dev/null)" == OK ]] \
  || die 'Redis SAVE failed under writer fence'
timeout --signal=TERM --kill-after=10s 60s docker stop --time 30 "${container_ids[postiz-redis]}" >/dev/null
for service in "${STOP_ORDER[@]}"; do
  [[ "$(docker inspect --format '{{.State.Running}}' "${container_ids[$service]}")" == false ]] \
    || die "writer did not stop: $service"
done
"$HELPER" update-quiesce-journal --journal "$JOURNAL" --phase stopped

client_connections=1
for _ in $(seq 1 30); do
  client_connections=$(pg_scalar postgres "SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND backend_type='client backend'")
  [[ "$client_connections" == 0 ]] && break
  sleep 1
done
prepared_transactions=$(pg_scalar postgres "SELECT count(*) FROM pg_prepared_xacts")
[[ "$client_connections" == 0 && "$prepared_transactions" == 0 ]] \
  || die 'database writers/connections remain after fence'
database_inventory_fenced=$(timeout 20s docker exec -e PGAPPNAME=freio-postiz-backup \
  "$PG_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 -U postiz -d postgres -Atc \
  "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1" 2>/dev/null)
[[ "$database_inventory_fenced" == "$expected_inventory" ]] \
  || die 'database inventory changed across the writer fence'
[[ "$(pg_scalar postgres "
  SELECT
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')
    +
    (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')")" == 0 ]] \
  || die 'maintenance postgres objects changed across the writer fence'

(
  ulimit -f $((MAX_GLOBALS_BYTES / 1024))
  timeout --signal=TERM --kill-after=10s 60s docker exec "${container_ids[postiz-postgres]}" \
    env PGAPPNAME=freio-postiz-backup pg_dumpall -U postiz --globals-only \
    > "$output_dir/globals.sql"
) || die 'bounded Postiz globals dump failed'
[[ -s "$output_dir/globals.sql" ]] || die 'Postiz globals dump is empty'
assert_database_identity() {
  [[ "$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.Name}}' \
      "${container_ids[postiz-postgres]}")" == \
     "${container_ids[postiz-postgres]}|${image_ids[postiz-postgres]}|true|/$POSTGRES_FENCED_NAME" ]] \
    || die 'Postgres container identity/state drifted during capture'
}
assert_database_identity
for database in "${DATABASES[@]}"; do
  (
    ulimit -f $((MAX_LOGICAL_DUMP_BYTES / 1024))
    timeout --signal=TERM --kill-after=10s 90s docker exec \
      -e PGAPPNAME=freio-postiz-backup \
      -e PGOPTIONS='-c statement_timeout=75000 -c lock_timeout=3000 -c TimeZone=UTC -c DateStyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3' \
      "${container_ids[postiz-postgres]}" pg_dump -U postiz -Fc -Z6 "$database" \
      > "$output_dir/database-$database.dump"
  ) || die "bounded database dump failed: $database"
  [[ -s "$output_dir/database-$database.dump" ]] || die "database dump is empty: $database"
  assert_database_identity
done

(
  ulimit -f $((MAX_PHYSICAL_ARCHIVE_BYTES / 1024))
  timeout --signal=TERM --kill-after=15s 150s docker exec "${container_ids[postiz-postgres]}" \
    env PGAPPNAME=freio-postiz-backup pg_basebackup -U postiz -D - -Ft -X fetch -z \
      --checkpoint=fast --manifest-checksums=SHA256 > "$output_dir/postgres-cluster.tar.gz"
) || die 'bounded physical Postgres capture failed'
gzip -t "$output_dir/postgres-cluster.tar.gz"
tar -tzf "$output_dir/postgres-cluster.tar.gz" PG_VERSION >/dev/null \
  || die 'physical Postgres capture lacks PG_VERSION'
assert_database_identity

runtime_config_source_bytes=0
for source in "${RUNTIME_CONFIG_SOURCES[@]}"; do
  source_path=/$source
  [[ -f "$source_path" && ! -L "$source_path" ]] \
    || die "runtime config source is missing or unsafe: $source"
  IFS=: read -r source_uid source_gid source_links source_bytes \
    < <(stat -Lc '%u:%g:%h:%s' -- "$source_path")
  [[ "$source_uid:$source_gid:$source_links" == 0:0:1 && "$source_bytes" =~ ^[0-9]+$ ]] \
    || die "runtime config source ownership/link contract failed: $source"
  ((source_bytes <= MAX_RUNTIME_CONFIG_MEMBER_BYTES)) \
    || die "runtime config source exceeds per-member byte ceiling: $source"
  runtime_config_source_bytes=$((runtime_config_source_bytes + source_bytes))
  ((runtime_config_source_bytes <= MAX_RUNTIME_CONFIG_EXPANDED_BYTES)) \
    || die 'runtime config sources exceed aggregate expanded byte ceiling'
done

(
  ulimit -f $((MAX_RUNTIME_ARCHIVE_BYTES / 1024))
  tar --no-recursion -czf "$output_dir/runtime-config.tar.gz" -C / \
    "${RUNTIME_CONFIG_SOURCES[@]}"
) || die 'bounded runtime config archive failed'
"$HELPER" verify-config-archive --archive "$output_dir/runtime-config.tar.gz"
"$HELPER" verify-config-source --archive "$output_dir/runtime-config.tar.gz"
verify_compose_generation writer-fenced
"$HELPER" verify-config-source --archive "$output_dir/runtime-config.tar.gz"
"$HELPER" config-archive-get --archive "$output_dir/runtime-config.tar.gz" \
  --key compose_sha256 > "$output_dir/runtime-config.compose.sha256"
"$HELPER" config-archive-get --archive "$output_dir/runtime-config.tar.gz" \
  --key dockerfile_sha256 > "$output_dir/runtime-config.dockerfile.sha256"
"$HELPER" config-archive-get --archive "$output_dir/runtime-config.tar.gz" \
  --key source_revision > "$output_dir/runtime-config.source-revision"
"$HELPER" seal-tree-archive --root "$CONFIG_VOLUME" --prefix postiz-config \
  --max-bytes $((64 * 1024 * 1024)) --max-members "$MAX_CONFIG_TREE_MEMBERS" \
  --output "$output_dir/config-volume.tar.gz"
"$HELPER" verify-tree-archive --archive "$output_dir/config-volume.tar.gz" \
  --prefix postiz-config --max-bytes $((64 * 1024 * 1024)) \
  --max-members "$MAX_CONFIG_TREE_MEMBERS"

redis_source=$REDIS_VOLUME/dump.rdb
[[ -d "$REDIS_VOLUME" && ! -L "$REDIS_VOLUME" ]] || die 'Redis volume root is unsafe'
[[ -f "$redis_source" && ! -L "$redis_source" && "$(stat -Lc '%h' "$redis_source")" == 1 ]] \
  || die 'stable Redis dump is missing or unsafe'
(( $(stat -Lc '%s' "$redis_source") <= MAX_REDIS_SOURCE_BYTES )) \
  || die 'stable Redis RDB exceeds byte ceiling'
redis_root_metadata=$(stat -Lc '%u:%g:%a' "$REDIS_VOLUME")
redis_rdb_metadata=$(stat -Lc '%u:%g:%a' "$redis_source")
cp --reflink=auto --preserve=mode,ownership,timestamps -- "$redis_source" "$output_dir/redis.rdb"
[[ -s "$output_dir/redis.rdb" && "$(head -c 5 "$output_dir/redis.rdb")" == REDIS ]] \
  || die 'stable Redis RDB copy failed'
redis_check_output=$output_dir/redis-check-rdb.txt
timeout --signal=TERM --kill-after=10s 120s \
  docker run --rm --name "postiz-capture-redis-check-$timestamp" \
  --label freio.postiz.capture-parser=true \
  --label "freio.postiz.capture-run=$timestamp" \
  --label freio.postiz.capture-role=redis-check \
  --memory 1g --memory-swap 1g --pids-limit 256 --cpus 1 \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  -v "$output_dir/redis.rdb:/backup/dump.rdb:ro" --entrypoint redis-check-rdb \
  "${image_ids[postiz-redis]}" /backup/dump.rdb > "$redis_check_output" 2>&1 \
  || die 'captured Redis RDB is invalid'
redis_rdb_keys=$(sed -nE 's/^\[info\] ([0-9]+) keys read$/\1/p' "$redis_check_output")
[[ "$redis_rdb_keys" =~ ^[0-9]+$ ]] || die 'Redis RDB checker key count is missing/ambiguous'
rm -f -- "$redis_check_output"

upload_source_bytes_fenced=$(tree_bytes "$UPLOAD_VOLUME")
upload_source_inodes_fenced=$(tree_inodes "$UPLOAD_VOLUME")
((upload_source_bytes_fenced <= MAX_UPLOAD_SOURCE_BYTES && \
  upload_source_inodes_fenced <= MAX_UPLOAD_SOURCE_INODES)) \
  || die 'writer-fenced uploads exceed byte/inode ceiling'
mkdir -m 755 "$output_dir/uploads-snapshot"
timeout --signal=TERM --kill-after=15s 150s cp -a --reflink=auto --one-file-system \
  "$UPLOAD_VOLUME/." "$output_dir/uploads-snapshot/"
"$HELPER" scan --root "$output_dir/uploads-snapshot" --timestamp "$timestamp" \
  --max-files 100000 --max-bytes $((16 * 1024 * 1024 * 1024)) \
  --output "$output_dir/uploads.json"

seal_operator_root() {
  local source=$1 prefix=$2 stem=$3
  "$HELPER" seal-tree-archive --root "$source" --prefix "$prefix" \
    --max-bytes $((512 * 1024 * 1024)) --max-members "$MAX_SEASONAL_TREE_MEMBERS" \
    --output "$output_dir/$stem.tar.gz"
  "$HELPER" verify-tree-archive --archive "$output_dir/$stem.tar.gz" --prefix "$prefix" \
    --max-bytes $((512 * 1024 * 1024)) --max-members "$MAX_SEASONAL_TREE_MEMBERS"
  printf 'present\n' > "$output_dir/$stem.status"
}
if [[ ! -e "$SEASONAL_POLICY" && ! -L "$SEASONAL_POLICY" ]]; then
  for root in /var/lib/freio-content/seasonal-releases \
      /var/lib/freio-content/seasonal-anchor-replacement; do
    [[ ! -e "$root" && ! -L "$root" ]] || die 'seasonal state exists without its required policy'
  done
  printf 'absent\n' > "$output_dir/seasonal-policy.status"
  printf 'absent\n' > "$output_dir/seasonal-releases.status"
  printf 'absent\n' > "$output_dir/seasonal-anchor-replacement.status"
else
  [[ -f "$SEASONAL_POLICY" && ! -L "$SEASONAL_POLICY" && \
     "$(stat -Lc '%u:%g:%a:%h' "$SEASONAL_POLICY")" == 0:0:600:1 ]] \
    || die 'seasonal backup policy is unsafe'
  "$HELPER" verify-seasonal-policy --policy "$SEASONAL_POLICY"
  cp --reflink=auto --preserve=mode,ownership,timestamps -- \
    "$SEASONAL_POLICY" "$output_dir/seasonal-policy.json"
  [[ "$(sha256sum "$SEASONAL_POLICY" | cut -d' ' -f1)" == \
     "$(sha256sum "$output_dir/seasonal-policy.json" | cut -d' ' -f1)" ]] \
    || die 'seasonal policy changed while copying'
  printf 'present\n' > "$output_dir/seasonal-policy.status"
  seal_operator_root /var/lib/freio-content/seasonal-releases \
    seasonal-releases seasonal-releases
  seal_operator_root /var/lib/freio-content/seasonal-anchor-replacement \
    seasonal-anchor-replacement seasonal-anchor-replacement
fi

declare -a count_args=()
add_count() { count_args+=(--count "$1=$2"); }
add_count cluster_roles "$(pg_scalar postgres 'SELECT count(*) FROM pg_roles')"
add_count cluster_role_memberships "$(pg_scalar postgres 'SELECT count(*) FROM pg_auth_members')"
add_count postiz_role_superuser "$(pg_scalar postgres "SELECT count(*) FROM pg_roles WHERE rolname='postiz' AND rolsuper")"
add_count postiz_role_login "$(pg_scalar postgres "SELECT count(*) FROM pg_roles WHERE rolname='postiz' AND rolcanlogin")"
add_count postiz_public_tables "$(pg_scalar postiz "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
add_count postiz_posts "$(pg_scalar postiz 'SELECT count(*) FROM "Post"')"
add_count postiz_integrations "$(pg_scalar postiz 'SELECT count(*) FROM "Integration"')"
add_count temporal_tables "$(pg_scalar temporal "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
add_count temporal_executions "$(pg_scalar temporal 'SELECT count(*) FROM executions')"
add_count temporal_current_executions "$(pg_scalar temporal 'SELECT count(*) FROM current_executions')"
add_count temporal_tasks "$(pg_scalar temporal 'SELECT count(*) FROM tasks')"
add_count temporal_schema_versions "$(pg_scalar temporal 'SELECT count(*) FROM schema_version')"
add_count visibility_tables "$(pg_scalar temporal_visibility "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
add_count visibility_executions "$(pg_scalar temporal_visibility 'SELECT count(*) FROM executions_visibility')"
add_count visibility_schema_versions "$(pg_scalar temporal_visibility 'SELECT count(*) FROM schema_version')"
add_count insights_tables "$(pg_scalar insights "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
add_count insights_post_insights "$(pg_scalar insights 'SELECT count(*) FROM post_insights')"
add_count insights_farm_pr_reviews "$(pg_scalar insights 'SELECT count(*) FROM farm_pr_reviews')"
add_count insights_farm_pr_triage "$(pg_scalar insights 'SELECT count(*) FROM farm_pr_triage')"
[[ "$(pg_scalar postiz \
  "SELECT CASE WHEN to_regclass('public.\"_prisma_migrations\"') IS NULL THEN 0 ELSE 1 END")" == 0 ]] \
  || die 'Postiz Prisma migration-table absence contract drifted'

role_fingerprint=$(pg_fingerprint postgres \
  "$("$HELPER" emit-fingerprint-sql --kind roles)")
role_membership_fingerprint=$(pg_fingerprint postgres \
  "$("$HELPER" emit-fingerprint-sql --kind role_memberships)")
declare -a catalog_fingerprint_args=()
for database in "${DATABASES[@]}"; do
  catalog_fingerprint_args+=(--catalog-fingerprint \
    "$database=$(pg_fingerprint "$database" "$("$HELPER" emit-fingerprint-sql --kind catalog)")")
done
declare -a migration_fingerprint_args=()
for migration_spec in \
    'temporal_schema_version|temporal' \
    'temporal_visibility_schema_version|temporal_visibility'; do
  IFS='|' read -r migration_name migration_database <<< "$migration_spec"
  migration_query=$("$HELPER" emit-fingerprint-sql --kind migration \
    --name "$migration_name" --database "$migration_database")
  migration_fingerprint_args+=(--migration-fingerprint \
    "$migration_name=$(pg_fingerprint "$migration_database" "$migration_query")")
done
assert_database_identity

"$HELPER" update-quiesce-journal --journal "$JOURNAL" --phase captured
recover_from_journal || die 'writer restart/readiness failed after capture'
capture_finished=$(date +%s)
((capture_finished - capture_started <= MAX_CAPTURE_SECONDS)) \
  || die 'writer quiescence exceeded its hard ceiling'
writer_args=()
for service in postiz-postgres "${STOP_ORDER[@]}"; do
  writer_args+=(--writer "$service|${container_ids[$service]}|${image_ids[$service]}")
done
"$HELPER" write-capture-evidence \
  --timestamp "$timestamp" \
  --started-epoch "$capture_started" \
  --finished-epoch "$capture_finished" \
  "${writer_args[@]}" \
  "${count_args[@]}" \
  "${catalog_fingerprint_args[@]}" \
  "${migration_fingerprint_args[@]}" \
  --role-fingerprint "$role_fingerprint" \
  --role-membership-fingerprint "$role_membership_fingerprint" \
  --postiz-prisma-migrations-absent \
  --redis-root-metadata "$redis_root_metadata" \
  --redis-rdb-metadata "$redis_rdb_metadata" \
  --postgres-user-objects "$postgres_user_objects" \
  --redis-rdb-keys "$redis_rdb_keys" \
  --upload-manifest "$output_dir/uploads.json" \
  --physical-cluster "$output_dir/postgres-cluster.tar.gz" \
  --output "$output_dir/capture.evidence.json"
find "$output_dir" -maxdepth 1 -type f -exec chmod 600 {} +
