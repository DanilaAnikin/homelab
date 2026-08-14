#!/usr/bin/env bash
# Serialize every operator-driven Postiz Compose mutation with recovery capture.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

readonly RUN_ROOT=/run/homelab-backup
readonly STATE_ROOT=/var/lib/homelab-backup
readonly MUTATION_LOCK=$RUN_ROOT/postiz-mutation.lock
readonly JOURNAL=$STATE_ROOT/postiz-quiesce-journal.json
readonly RECOVER=/usr/local/sbin/postiz-quiesced-capture.sh
readonly COMPOSE=/srv/postiz/docker-compose.yml
readonly ENV_FILE=/srv/postiz/postiz.env

die() { printf 'Postiz locked Compose: %s\n' "$*" >&2; exit 1; }
safe_root_file() {
  local path=$1 mode=$2
  [[ -f "$path" && ! -L "$path" && \
     "$(stat -Lc '%u:%g:%a:%h' -- "$path")" == "0:0:${mode}:1" ]] \
    || die "trusted file contract failed: $path"
}
((EUID == 0)) || die 'must run as root'
(($# >= 1)) || die 'usage: postiz-compose-locked.sh <up|down|start|stop|restart|create|rm|pull|build> [args]'
case "$1" in
  up|down|start|stop|restart|create|rm|pull|build) ;;
  *) die 'unsupported Compose mutation command' ;;
esac
safe_root_file "$RECOVER" 755
safe_root_file "$COMPOSE" 644
safe_root_file "$ENV_FILE" 600
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || die 'backup RuntimeDirectory is unsafe'
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && \
   "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || die 'backup StateDirectory is unsafe'
[[ -f "$MUTATION_LOCK" && ! -L "$MUTATION_LOCK" && \
   "$(stat -Lc '%u:%g:%a:%h' "$MUTATION_LOCK")" == 0:0:600:1 ]] \
  || die 'mutation lock is unsafe'

exec 9<>"$MUTATION_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$MUTATION_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/9")" ]] \
  || die 'mutation lock descriptor/path drifted'

# Journal absence is checked only while holding the same mutation lease used by
# capture. If a capture failed while this wrapper waited, release the lease,
# recover exact IDs, and retry. This closes the precheck-to-flock race.
for attempt in 1 2 3; do
  flock -w 300 9 || die 'Postiz recovery/mutation lock is busy'
  if [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]; then
    exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"
  fi
  flock -u 9
  timeout --signal=TERM --kill-after=15s 480s "$RECOVER" --recover-only \
    || die 'stale writer-fence recovery failed'
done
die 'writer-fence journal persisted across bounded recovery retries'
