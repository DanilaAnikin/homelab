#!/usr/bin/env bash
# Generate a short-lived semantic attestation from the Cloudflare R2 read API.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py
readonly SOURCE=/srv/homelab/secrets/postiz-r2-policy-source.json
readonly R2_CONF=/srv/homelab/secrets/rclone.conf
readonly WORKSPACE_CLEANUP=/usr/local/sbin/postiz-backup-workspace-cleanup.sh
readonly OUTPUT=/var/lib/homelab-backup/postiz-storage-policy.json
readonly STATE_ROOT=/var/lib/homelab-backup
readonly RUN_ROOT=/run/homelab-backup
readonly LOCK=$RUN_ROOT/postiz-policy-attest.lock
readonly API_ROOT=https://api.cloudflare.com/client/v4
readonly MAX_POLICY_RESPONSE_BYTES=$((1024 * 1024))

die() { printf 'Postiz R2 policy attestation: %s\n' "$*" >&2; exit 1; }
temporary() {
  printf 'Postiz R2 policy attestation: Cloudflare control plane temporarily unavailable\n' >&2
  exit 75
}

safe_root_file() {
  local path=$1 mode=$2
  [[ -f "$path" && ! -L "$path" && "$(stat -Lc '%u:%g:%a:%h' "$path")" == "0:0:${mode}:1" ]] \
    || die "trusted file contract failed: $path"
}

((EUID == 0)) || die 'must run as root'
safe_root_file "$HELPER" 755
safe_root_file "$SOURCE" 600
safe_root_file "$R2_CONF" 600
safe_root_file "$WORKSPACE_CLEANUP" 755
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || die 'backup StateDirectory is unsafe'
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || die 'backup RuntimeDirectory is unsafe'
safe_root_file "$LOCK" 600
command -v curl >/dev/null || die 'curl is missing'
command -v rclone >/dev/null || die 'rclone is missing'
"$HELPER" verify-rclone-source --source "$SOURCE" --rclone-config "$R2_CONF"

exec 9<>"$LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/9")" ]] \
  || die 'policy lock descriptor/path drifted'
flock -x 9
"$WORKSPACE_CLEANUP" --scope policy --lock-held-fd 9

work=$(mktemp -d "$STATE_ROOT/postiz-policy.XXXXXX") \
  || die 'cannot create R2 policy workspace'
cleanup() { rm -rf --one-file-system -- "$work"; }
trap cleanup EXIT

declare -A account_ids=() buckets=() token_files=()
for label in primary dr; do
  account_ids[$label]=$("$HELPER" storage-source-get --source "$SOURCE" --remote "$label" --key account_id)
  buckets[$label]=$("$HELPER" storage-source-get --source "$SOURCE" --remote "$label" --key bucket)
  token_files[$label]=$("$HELPER" storage-source-get --source "$SOURCE" --remote "$label" --key policy_token_file)
  safe_root_file "${token_files[$label]}" 600
done

clean_rclone() {
  timeout --signal=TERM --kill-after=10s 120s \
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent \
      rclone --config "$R2_CONF" "$@"
}

# Object R&W credentials include DeleteObject, so their bucket boundary matters.
# Prove own-bucket list access and cross-bucket denial without displaying keys or objects.
clean_rclone lsf r2postiz:homelab-backups --max-depth 1 \
  >/dev/null 2>&1 || die 'primary runtime credential cannot access its own bucket'
clean_rclone lsf r2drpostiz:homelab-backups-dr --max-depth 1 \
  >/dev/null 2>&1 || die 'DR runtime credential cannot access its own bucket'
prove_access_denied() {
  local remote=$1 label=$2 evidence="$work/cross-scope-$label.txt"
  if (
    ulimit -f $((MAX_POLICY_RESPONSE_BYTES / 1024))
    clean_rclone lsf "$remote" --max-depth 1 > /dev/null 2> "$evidence"
  ); then
    die "$label runtime credential unexpectedly reaches the other bucket"
  fi
  if ! grep -Eiq 'access.?denied|forbidden|(^|[^0-9])403([^0-9]|$)' "$evidence"; then
    die "$label cross-bucket probe was not an explicit authorization denial"
  fi
}
prove_access_denied r2postiz:homelab-backups-dr primary
prove_access_denied r2drpostiz:homelab-backups dr

fetch_policy() {
  local label=$1 resource=$2 destination=$3 token_file token extra token_fd url
  local http_code_file http_code curl_rc
  token_file=${token_files[$label]}
  exec {token_fd}<"$token_file"
  [[ ! -L "$token_file" && "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$token_file")" == \
     "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/$token_fd")" ]] \
    || die 'read-only policy token descriptor/path drifted'
  token=
  IFS= read -r token <&"$token_fd" || [[ -n "$token" ]] \
    || die 'read-only policy token file is empty'
  extra=
  if IFS= read -r extra <&"$token_fd" || [[ -n "$extra" ]]; then
    die 'read-only policy token file contains more than one line'
  fi
  exec {token_fd}<&-
  [[ "$token" =~ ^[A-Za-z0-9._~-]{20,256}$ ]] || die 'read-only policy token has an invalid representation'
  url="$API_ROOT/accounts/${account_ids[$label]}/r2/buckets/${buckets[$label]}/$resource"
  http_code_file="$work/$label-$resource.http-code"
  # The bearer is sent only through an anonymous stdin curl config. It is never
  # exported, placed in argv, or included in success/failure output.
  curl_rc=0
  {
    printf 'silent\nshow-error\nfail-with-body\nproto = "=https"\nnoproxy = "*"\nconnect-timeout = 10\nmax-time = 30\n'
    printf 'request = "GET"\nurl = "%s"\n' "$url"
    printf 'header = "Accept: application/json"\nheader = "Authorization: Bearer %s"\n' "$token"
    printf 'output = "%s"\n' "$destination"
    printf 'write-out = "%%{http_code}\\n"\n'
  } | (
    ulimit -f $((MAX_POLICY_RESPONSE_BYTES / 1024))
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
      curl --disable --config - > "$http_code_file" 2>/dev/null
  ) || curl_rc=$?
  http_code=$(tr -d '\r\n' < "$http_code_file")
  [[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code=000
  if ((curl_rc != 0)); then
    case "$http_code:$curl_rc" in
      429:*|5??:*|000:5|000:6|000:7|000:28|000:35|000:52|000:55|000:56)
        temporary
        ;;
      *) die "Cloudflare $label $resource read failed" ;;
    esac
  fi
  [[ "$http_code" == 2?? ]] || die "Cloudflare $label $resource returned a non-success response"
  [[ -s "$destination" && ! -L "$destination" && \
     "$(stat -Lc '%s' "$destination")" -le "$MAX_POLICY_RESPONSE_BYTES" ]] \
    || die 'Cloudflare policy response is missing or exceeds its byte ceiling'
}

for label in primary dr; do
  fetch_policy "$label" lock "$work/$label-lock.json"
  fetch_policy "$label" lifecycle "$work/$label-lifecycle.json"
done

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
"$HELPER" attest-storage-policy \
  --source "$SOURCE" \
  --timestamp "$timestamp" \
  --primary-lock "$work/primary-lock.json" \
  --primary-lifecycle "$work/primary-lifecycle.json" \
  --dr-lock "$work/dr-lock.json" \
  --dr-lifecycle "$work/dr-lifecycle.json" \
  --output "$OUTPUT"
"$HELPER" verify-storage-policy --policy "$OUTPUT"
