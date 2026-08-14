#!/usr/bin/env bash
# Reap only stale, root-owned backup workspaces while their exact producer lock is held.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

die() { printf 'Postiz backup workspace cleanup: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'usage: %s --scope nightly|frequent|artifact|policy|all [--lock-held-fd N]\n' "$0" >&2
  exit 64
}

scope=
held_fd=
test_root=
while (($#)); do
  case "$1" in
    --scope) (($# >= 2)) || usage; scope=$2; shift 2 ;;
    --lock-held-fd) (($# >= 2)) || usage; held_fd=$2; shift 2 ;;
    --test-root) (($# >= 2)) || usage; test_root=$2; shift 2 ;;
    *) usage ;;
  esac
done
case "$scope" in nightly|frequent|artifact|policy|all) ;; *) usage ;; esac
[[ -z "$held_fd" || "$held_fd" =~ ^[0-9]+$ ]] || usage
[[ "$scope" != all || -z "$held_fd" ]] || usage
if [[ -n "$test_root" ]]; then
  # Non-privileged integration-test mode exercises the real CLI/reaper against
  # a caller-owned temporary tree. Root can never redirect production paths.
  ((EUID != 0)) || die 'test-root mode is refused for root'
  [[ "$test_root" == /* && -d "$test_root" && ! -L "$test_root" && \
     "$(readlink -f -- "$test_root")" == "$test_root" && \
     "$(stat -Lc '%u:%g:%a' "$test_root")" == "$EUID:$(id -g):700" ]] \
    || die 'test root is unsafe'
  STATE_ROOT=$test_root/state
  RUN_ROOT=$test_root/run
  expected_uid=$EUID
  expected_gid=$(id -g)
else
  ((EUID == 0)) || die 'must run as root'
  STATE_ROOT=/var/lib/homelab-backup
  RUN_ROOT=/run/homelab-backup
  expected_uid=0
  expected_gid=0
fi
readonly STATE_ROOT RUN_ROOT expected_uid expected_gid
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && \
   "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == "$expected_uid:$expected_gid:700" ]] \
  || die 'backup StateDirectory is unsafe'
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && \
   "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == "$expected_uid:$expected_gid:700" ]] \
  || die 'backup RuntimeDirectory is unsafe'
command -v flock >/dev/null || die 'flock is missing'
command -v findmnt >/dev/null || die 'findmnt is missing'
command -v mountpoint >/dev/null || die 'mountpoint is missing'

scope_contract() {
  case "$1" in
    nightly) printf '%s\t%s\n' "$RUN_ROOT/nightly-workspace.lock" nightly ;;
    frequent) printf '%s\t%s\n' "$RUN_ROOT/frequent-workspace.lock" frequent ;;
    artifact) printf '%s\t%s\n' "$RUN_ROOT/postiz-artifact.lock" postiz-artifact ;;
    policy) printf '%s\t%s\n' "$RUN_ROOT/postiz-policy-attest.lock" postiz-policy ;;
    *) die 'unknown cleanup scope' ;;
  esac
}

reap_prefix() {
  local prefix=$1 candidate basename mount_count state_device candidate_device
  state_device=$(stat -Lc '%d' "$STATE_ROOT")
  shopt -s nullglob
  local -a candidates=("$STATE_ROOT"/"$prefix".??????)
  shopt -u nullglob
  for candidate in "${candidates[@]}"; do
    basename=${candidate##*/}
    [[ "$basename" =~ ^${prefix}\.[A-Za-z0-9]{6}$ && \
       -d "$candidate" && ! -L "$candidate" && \
       "$(stat -Lc '%u:%g:%a' "$candidate")" == "$expected_uid:$expected_gid:700" ]] \
      || die "unsafe stale workspace candidate for scope $scope"
    candidate_device=$(stat -Lc '%d' "$candidate")
    [[ "$candidate_device" == "$state_device" ]] \
      || die "stale workspace crosses the StateDirectory filesystem for scope $scope"
    if mountpoint -q -- "$candidate"; then
      die "stale workspace is a mountpoint for scope $scope"
    fi
    mount_count=$(findmnt -rn -o TARGET | awk -v root="$candidate" '
      $0 == root || index($0, root "/") == 1 { count++ }
      END { print count + 0 }
    ')
    [[ "$mount_count" == 0 ]] \
      || die "stale workspace contains a nested mount for scope $scope"
    rm -rf --one-file-system -- "$candidate"
    [[ ! -e "$candidate" && ! -L "$candidate" ]] \
      || die "stale workspace removal was incomplete for scope $scope"
  done
  sync -f "$STATE_ROOT"
}

cleanup_scope() {
  local selected=$1 lock prefix fd path_meta fd_meta
  IFS=$'\t' read -r lock prefix < <(scope_contract "$selected")
  [[ -f "$lock" && ! -L "$lock" && \
     "$(stat -Lc '%u:%g:%a:%h' "$lock")" == "$expected_uid:$expected_gid:600:1" ]] \
    || die "workspace lock is missing or unsafe for scope $selected"
  if [[ -n "$held_fd" ]]; then
    fd=$held_fd
    [[ -e "/proc/$$/fd/$fd" ]] || die 'declared inherited lock FD is closed'
    path_meta=$(stat -Lc '%u:%g:%a:%h:%d:%i' "$lock")
    fd_meta=$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/$fd")
    [[ "$path_meta" == "$fd_meta" ]] || die 'workspace lock descriptor/path drifted'
    flock -n "$fd" || die 'declared inherited workspace lock is not available'
    reap_prefix "$prefix"
    return
  fi
  exec {fd}<>"$lock"
  path_meta=$(stat -Lc '%u:%g:%a:%h:%d:%i' "$lock")
  fd_meta=$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/$fd")
  [[ "$path_meta" == "$fd_meta" ]] || die 'workspace cleanup lock descriptor/path drifted'
  if ! flock -n "$fd"; then
    printf 'Postiz backup workspace cleanup: active %s workspace left untouched\n' "$selected" >&2
    exec {fd}>&-
    return
  fi
  reap_prefix "$prefix"
  flock -u "$fd"
  exec {fd}>&-
}

if [[ "$scope" == all ]]; then
  for selected in nightly frequent artifact policy; do
    cleanup_scope "$selected"
  done
else
  cleanup_scope "$scope"
fi
