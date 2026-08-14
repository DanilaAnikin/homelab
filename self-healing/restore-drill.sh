#!/usr/bin/env bash
# ============================================================================
# restore-drill.sh — týdenní ověření, že šifrované zálohy JSOU obnovitelné.
# „Netestovaný dump není záloha." Stáhne nejnovější .enc z R2, dešifruje, obnoví
# do IZOLOVANÉHO throwaway postgresu, ověří schema + data (řádky), uklidí, hlásí
# na Telegram. NIKDY nesahá na produkční DB/volumes — vlastní dočasný kontejner.
# ============================================================================
set -uo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
readonly BACKUP_KEY=/srv/homelab/secrets/freio-backup-key.txt
readonly R2CONF=/srv/homelab/secrets/rclone.conf
readonly NIGHTLY="r2:homelab-backups/nightly/$(date -u +%Y-%m)"
readonly DRILL_IMG="supabase/postgres:17.6.1.136@sha256:f371b5f3f2ac0a05703f33d6e6134515fb2498cab708fb948a0aeb7481467c00"
readonly POSTIZ_DRILL=/srv/homelab/self-healing/postiz-restore-drill.sh
readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py
readonly NOTIFY=/srv/homelab/self-healing/notify.sh
readonly STATE_ROOT=/var/lib/homelab-backup
readonly RUN_ROOT=/run/homelab-backup
readonly GENERIC_LOCK=$RUN_ROOT/generic-restore.lock
readonly GENERIC_JOURNAL=$STATE_ROOT/generic-restore-active.json
readonly LOG=$STATE_ROOT/restore-drill.log
readonly DBS="freio ripieno lokwave"
readonly MAX_GENERIC_CIPHER_BYTES=$((2 * 1024 * 1024 * 1024))
readonly MAX_GENERIC_PLAIN_BYTES=$((2 * 1024 * 1024 * 1024))
readonly MAX_REMOTE_LIST_BYTES=$((2 * 1024 * 1024))
readonly GENERIC_PGDATA_BYTES=$((8 * 1024 * 1024 * 1024))
readonly MIN_STATE_MARGIN_BYTES=$((2 * 1024 * 1024 * 1024))
readonly MIN_DOCKER_FREE_BYTES=$((2 * 1024 * 1024 * 1024))
readonly MIN_AVAILABLE_MEMORY_BYTES=$((12 * 1024 * 1024 * 1024))
readonly -a RC=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent
  rclone --config "$R2CONF" --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G)

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ printf 'Generic restore drill: %s\n' "$*" >&2; exit 1; }
usage(){ printf 'usage: %s [--cleanup-only]\n' "$0" >&2; exit 64; }
safe_root_file(){
  local path=$1 mode=$2
  [[ -f "$path" && ! -L "$path" && \
     "$(stat -Lc '%u:%g:%a:%h' "$path")" == "0:0:${mode}:1" ]]
}
RESULTS=""; FAIL=0
notify(){ printf '%s\n' "$1" | "$NOTIFY"; }

cleanup_only=0
case $# in
  0) ;;
  1) [[ "$1" == --cleanup-only ]] || usage; cleanup_only=1 ;;
  *) usage ;;
esac

((EUID == 0)) || die 'must run as root'
safe_root_file "$HELPER" 755 || die 'manifest helper is missing or unsafe'
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && \
   "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || die 'backup StateDirectory is unsafe'
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && \
   "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || die 'backup RuntimeDirectory is unsafe'
safe_root_file "$GENERIC_LOCK" 600 || die 'generic restore lock is missing or unsafe'
command -v docker >/dev/null || die 'docker is missing'

exec 8<>"$GENERIC_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$GENERIC_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/8")" ]] \
  || die 'generic restore lock descriptor/path drifted'
flock -n 8 || die 'another generic restore drill is running'

reap_generic_restore(){
  local stale_work run_id container rows listed_id listed_name listed_extra inspection
  local candidate basename
  if [[ -e "$GENERIC_JOURNAL" || -L "$GENERIC_JOURNAL" ]]; then
    safe_root_file "$GENERIC_JOURNAL" 600 || die 'generic restore journal is unsafe'
    stale_work=$("$HELPER" generic-restore-journal-get --journal "$GENERIC_JOURNAL" \
      --key work_directory) || die 'cannot read generic restore workspace journal'
    run_id=$("$HELPER" generic-restore-journal-get --journal "$GENERIC_JOURNAL" \
      --key run_id) || die 'cannot read generic restore run ID'
    container=$("$HELPER" generic-restore-journal-get --journal "$GENERIC_JOURNAL" \
      --key container) || die 'cannot read generic restore container journal'
    [[ "$stale_work" == "$STATE_ROOT/restore-generic.$run_id" && \
       "$run_id" =~ ^[A-Za-z0-9]{6}$ ]] || die 'generic restore journal path differs'
    rows=$(timeout --signal=TERM --kill-after=5s 30s docker ps -a --no-trunc \
      --filter "name=^/${container}$" --format '{{.ID}}|{{.Names}}' 2>/dev/null) \
      || die 'cannot prove generic restore container presence/absence'
    [[ "$rows" != *$'\n'* ]] || die 'generic restore container name is not unique'
    if [[ -n "$rows" ]]; then
      IFS='|' read -r listed_id listed_name listed_extra <<< "$rows"
      [[ "$listed_id" =~ ^[0-9a-f]{64}$ && "$listed_name" == "$container" && \
         -z "$listed_extra" ]] || die 'generic restore container listing is invalid'
      inspection=$(timeout --signal=TERM --kill-after=5s 30s docker inspect --format \
        '{{.Name}}|{{with .Config.Labels}}{{index . "freio.generic.restore-run"}}{{end}}|{{with .Config.Labels}}{{index . "freio.generic.restore-role"}}{{end}}' \
        "$listed_id" 2>/dev/null) || die 'cannot inspect generic restore container'
      [[ "$inspection" == "/$container|$run_id|postgres" ]] \
        || die 'generic restore container identity/label differs'
      timeout --signal=TERM --kill-after=10s 30s docker rm -f "$listed_id" >/dev/null \
        || die 'cannot remove generic restore container'
    fi
    if [[ -e "$stale_work" || -L "$stale_work" ]]; then
      [[ -d "$stale_work" && ! -L "$stale_work" && \
         "$(stat -Lc '%u:%g:%a' "$stale_work")" == 0:0:700 ]] \
        || die 'generic restore workspace is unsafe'
      rm -rf --one-file-system -- "$stale_work"
    fi
    rm -f -- "$GENERIC_JOURNAL"
    sync -f "$STATE_ROOT"
  fi
  shopt -s nullglob
  for candidate in "$STATE_ROOT"/restore-generic.??????; do
    basename=${candidate##*/}
    [[ "$basename" =~ ^restore-generic\.[A-Za-z0-9]{6}$ && \
       -d "$candidate" && ! -L "$candidate" && \
       "$(stat -Lc '%u:%g:%a' "$candidate")" == 0:0:700 ]] \
      || die 'orphan generic restore workspace is unsafe'
    rm -rf --one-file-system -- "$candidate"
  done
  shopt -u nullglob
}

reap_generic_restore
if ((cleanup_only)); then
  safe_root_file "$POSTIZ_DRILL" 750 || die 'Postiz cleanup helper is missing or unsafe'
  exec "$POSTIZ_DRILL" --cleanup-only
fi

safe_root_file "$BACKUP_KEY" 600 && [[ -s "$BACKUP_KEY" ]] \
  || die 'backup key is missing or unsafe'
safe_root_file "$R2CONF" 600 || die 'rclone config is missing or unsafe'
safe_root_file "$POSTIZ_DRILL" 750 || die 'Postiz restore drill is missing or unsafe'
docker image inspect "$DRILL_IMG" >/dev/null 2>&1 || { echo "chybí pinned restore image"; exit 1; }

WORK=$(mktemp -d "$STATE_ROOT/restore-generic.XXXXXX") || die 'cannot create generic restore workspace'
run_id=${WORK##*.}
"$HELPER" write-generic-restore-journal --timestamp "$(date -u +%Y%m%dT%H%M%SZ)" \
  --run-id "$run_id" --output "$GENERIC_JOURNAL" \
  || die 'cannot persist generic restore journal'
DRILL_CT=$("$HELPER" generic-restore-journal-get --journal "$GENERIC_JOURNAL" \
  --key container) || die 'cannot read generic restore container name'

cleanup(){ reap_generic_restore; }
on_exit(){
  local rc=$?
  trap - EXIT
  cleanup || rc=1
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

listing=$WORK/nightly-files.txt
(
  ulimit -f $((MAX_REMOTE_LIST_BYTES / 1024))
  timeout --signal=TERM --kill-after=10s 120s \
    "${RC[@]}" lsf "$NIGHTLY/" --max-depth 1 --files-only \
      --format sp --separator '|' > "$listing"
) || die 'bounded generic backup listing failed'
listing_bytes=$(stat -Lc '%s' "$listing")
listing_lines=$(wc -l < "$listing")
((listing_bytes <= MAX_REMOTE_LIST_BYTES && listing_lines <= 10000)) \
  || die 'generic backup listing exceeds byte/entry ceiling'

declare -A selected_name=() selected_size=()
selected_cipher_total=0
for db in $DBS; do
  best_name=
  best_timestamp=
  best_size=
  while IFS='|' read -r candidate_size candidate_name candidate_extra; do
    [[ -z "$candidate_extra" && \
       "$candidate_name" =~ ^db_([A-Za-z0-9.-]+_)?${db}_([0-9]{8}T[0-9]{6}Z)\.dump\.enc$ ]] \
      || continue
    [[ "$candidate_size" =~ ^[0-9]+$ && "$candidate_size" -gt 0 && \
       "$candidate_size" -le "$MAX_GENERIC_CIPHER_BYTES" ]] \
      || die "generic backup object has invalid or excessive size: $db"
    candidate_timestamp=${BASH_REMATCH[2]}
    if [[ -z "$best_timestamp" || "$candidate_timestamp" > "$best_timestamp" ]]; then
      best_timestamp=$candidate_timestamp
      best_name=$candidate_name
      best_size=$candidate_size
    fi
  done < "$listing"
  if [[ -z "$best_name" ]]; then
    RESULTS+="❌ ${db}: žádná bounded záloha na R2\n"
    FAIL=1
    continue
  fi
  selected_name[$db]=$best_name
  selected_size[$db]=$best_size
  selected_cipher_total=$((selected_cipher_total + best_size))
done

required_state_bytes=$((selected_cipher_total * 2 + MIN_STATE_MARGIN_BYTES))
state_free_bytes=$(df -PB1 --output=avail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
state_free_inodes=$(df -Pi --output=iavail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
docker_root=$(docker info --format '{{.DockerRootDir}}')
[[ "$docker_root" == /* && -d "$docker_root" && ! -L "$docker_root" ]] \
  || die 'Docker data root is unsafe'
docker_free_bytes=$(df -PB1 --output=avail "$docker_root" | tail -1 | tr -d '[:space:]')
docker_free_inodes=$(df -Pi --output=iavail "$docker_root" | tail -1 | tr -d '[:space:]')
available_memory_kib=$(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo)
[[ "$state_free_bytes" =~ ^[0-9]+$ && "$state_free_inodes" =~ ^[0-9]+$ && \
   "$docker_free_bytes" =~ ^[0-9]+$ && "$docker_free_inodes" =~ ^[0-9]+$ && \
   "$available_memory_kib" =~ ^[0-9]+$ ]] || die 'generic restore resource preflight is unavailable'
if [[ "$(stat -Lc '%d' "$STATE_ROOT")" == "$(stat -Lc '%d' "$docker_root")" ]]; then
  ((state_free_bytes >= required_state_bytes + MIN_DOCKER_FREE_BYTES && \
    state_free_inodes >= 20000)) \
    || die 'shared generic restore/Docker filesystem preflight failed'
else
  ((state_free_bytes >= required_state_bytes && state_free_inodes >= 10000)) \
    || die 'generic restore StateDirectory byte/inode preflight failed'
  ((docker_free_bytes >= MIN_DOCKER_FREE_BYTES && docker_free_inodes >= 10000)) \
    || die 'generic restore Docker-root byte/inode preflight failed'
fi
((available_memory_kib * 1024 >= MIN_AVAILABLE_MEMORY_BYTES)) \
  || die 'generic restore available-memory preflight failed'

# ── throwaway postgres (izolovaný, žádná prod síť, žádný host port) ──────────
docker run -d --name "$DRILL_CT" --memory 10g --memory-swap 10g --pids-limit 512 --cpus 2 \
  --label "freio.generic.restore-run=$run_id" --label freio.generic.restore-role=postgres \
  --network none --log-driver none \
  --tmpfs "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=$GENERIC_PGDATA_BYTES,mode=0700" \
  --mount "type=bind,src=$WORK,dst=/restore,readonly" \
  -e POSTGRES_PASSWORD=drill "$DRILL_IMG" >/dev/null \
  || die 'cannot start generic restore container'
log "throwaway postgres startuje…"
ready=0
for _ in $(seq 1 45); do
  if docker exec "$DRILL_CT" pg_isready -U postgres >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[[ $ready -eq 1 ]] || { notify "🔄 Restore drill ❌ throwaway postgres nenaběhl"; exit 1; }
sleep 3   # doběhnutí supabase init (role/extenze)

# ── per-DB: stáhni → dešifruj → restore → ověř ──────────────────────────────
for db in $DBS; do
  f=${selected_name[$db]:-}
  [[ -n "$f" ]] || continue
  expected_cipher_size=${selected_size[$db]}

  if ! (
    ulimit -f $((MAX_GENERIC_CIPHER_BYTES / 1024))
    timeout --signal=TERM --kill-after=10s 900s \
      "${RC[@]}" copyto "$NIGHTLY/$f" "$WORK/$db.enc" -q
  ); then
    RESULTS+="❌ ${db}: stažení selhalo\n"; FAIL=1; continue; fi
  actual_cipher_size=$(stat -Lc '%s' "$WORK/$db.enc")
  if [[ "$actual_cipher_size" != "$expected_cipher_size" ]]; then
    RESULTS+="❌ ${db}: stažená velikost se liší\n"; FAIL=1
    rm -f -- "$WORK/$db.enc"
    continue
  fi
  if ! (
    ulimit -f $((MAX_GENERIC_PLAIN_BYTES / 1024))
    timeout --signal=TERM --kill-after=10s 600s \
      openssl enc -d -aes-256-cbc -pbkdf2 -in "$WORK/$db.enc" \
        -out "$WORK/$db.dump" -pass file:"$BACKUP_KEY" 2>/dev/null
  ) || [[ ! -s "$WORK/$db.dump" ]] || \
     (( $(stat -Lc '%s' "$WORK/$db.dump") > MAX_GENERIC_PLAIN_BYTES )); then
    RESULTS+="❌ ${db}: DEŠIFROVÁNÍ selhalo (klíč?)\n"; FAIL=1; continue; fi

  docker exec "$DRILL_CT" psql -U postgres -q -c "DROP DATABASE IF EXISTS d_$db" >/dev/null 2>&1
  docker exec "$DRILL_CT" psql -U postgres -q -c "CREATE DATABASE d_$db" >/dev/null 2>&1
  # pg_restore smí mít nefatální chyby (chybějící role/grant) — úspěch měříme daty, ne exit kódem
  timeout --signal=TERM --kill-after=10s 900s docker exec "$DRILL_CT" \
    pg_restore --no-owner --no-privileges -U postgres -d "d_$db" "/restore/$db.dump" \
    >/dev/null 2>&1 || true
  docker exec "$DRILL_CT" psql -U postgres -d "d_$db" -q -c "ANALYZE" >/dev/null 2>&1

  ntab=$(docker exec "$DRILL_CT" psql -U postgres -d "d_$db" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'" 2>/dev/null | tr -d '[:space:]')
  big=$(docker exec "$DRILL_CT" psql -U postgres -d "d_$db" -tAc \
    "SELECT schemaname||'.'||relname||':'||n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]')
  bign=${big##*:}

  if [[ "$ntab" =~ ^[0-9]+$ && "$ntab" -gt 0 && "$bign" =~ ^[0-9]+$ && "$bign" -gt 0 ]]; then
    RESULTS+="✅ ${db}: ${ntab} tab., ${big}\n"
    log "✅ $db OK (${ntab} tab, ${big})"
  else
    RESULTS+="❌ ${db}: restore neověřen (tab=${ntab} big=${big})\n"; FAIL=1
    log "❌ $db FAIL (tab=$ntab big=$big)"
  fi
  timeout --signal=TERM --kill-after=5s 30s docker exec "$DRILL_CT" \
    psql -U postgres -q -c "DROP DATABASE IF EXISTS d_$db" >/dev/null 2>&1 \
    || die "cannot release bounded generic restore database: $db"
  rm -f -- "$WORK/$db.enc" "$WORK/$db.dump"
done

# The generic plaintext/container stage is fully gone before the longer Postiz
# drill starts.  A later SIGKILL is therefore handled only by the Postiz journal.
cleanup || die 'generic restore cleanup failed before Postiz drill'

# Postiz má vlastní recovery-set commit marker a navíc obnovuje root-only config,
# uploads i všechny čtyři exact Docker image archives nezávisle z primary a DR. Skript sám používá
# pouze throwaway kontejnery s `--network none` a nedotýká se prod volumes.
if "$POSTIZ_DRILL"; then
  RESULTS+="✅ postiz: globals + 4 DB + Redis/config/uploads/state + 4 images z primary i DR\n"
else
  RESULTS+="❌ postiz: recovery-set restore selhal\n"
  FAIL=1
fi

MSG=$(echo -e "$RESULTS")
{ echo "═══ RESTORE DRILL $(date -Iseconds) ═══"; echo -e "$RESULTS"; } >> "$LOG"
if [[ $FAIL -eq 0 ]]; then
  notify "🔄 Restore drill ✅ všechny zálohy obnovitelné:"$'\n'"$MSG" || true
  exit 0
else
  notify "🔄 Restore drill ❌ PROBLÉM s obnovitelností:"$'\n'"$MSG" || true
  exit 1
fi
