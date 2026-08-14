#!/usr/bin/env bash
# Encrypted Postiz uploads + exact Docker-image archives to server-locked primary and DR R2.
set -Eeuo pipefail
umask 0077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py
readonly BACKUP_KEY=/srv/homelab/secrets/freio-backup-key.txt
readonly R2_CONF=/srv/homelab/secrets/rclone.conf
readonly POLICY_ATTESTER=/usr/local/sbin/postiz-r2-policy-attest.sh
readonly WORKSPACE_CLEANUP=/usr/local/sbin/postiz-backup-workspace-cleanup.sh
readonly STORAGE_POLICY=/var/lib/homelab-backup/postiz-storage-policy.json
readonly PRIMARY=r2postiz:homelab-backups/postiz
readonly DR=r2drpostiz:homelab-backups-dr/postiz
readonly BLOB_PREFIX=uploads/blobs/sha256
readonly STATE_ROOT=/var/lib/homelab-backup
readonly RUN_ROOT=/run/homelab-backup
readonly ARTIFACT_LOCK=$RUN_ROOT/postiz-artifact.lock
readonly LIVE_UPLOAD_ROOT=/var/lib/docker/volumes/postiz_postiz-uploads/_data
readonly COMPOSE=/srv/postiz/docker-compose.yml
readonly DOCKERFILE=/srv/postiz/Dockerfile.patch
readonly -a IMAGE_SPECS=(
  'postiz|postiz-freio:patched'
  'postiz-postgres|postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193'
  'postiz-redis|redis:7.2@sha256:6372db89351b00ba0ddca437ff49ce2ed4beed8a961a27d8259060c9603c240d'
  'postiz-temporal|temporalio/auto-setup:1.25.2@sha256:b1edc1e20002d958c8182f2ae08dee877a125083683a627a44917683419ba6a8'
)
readonly MAX_FILES=100000
readonly MAX_SOURCE_BYTES=$((16 * 1024 * 1024 * 1024))
readonly MAX_NEW_CIPHER_BYTES=$((8 * 1024 * 1024 * 1024))
readonly MAX_IMAGE_CIPHER_BYTES=$((5 * 1024 * 1024 * 1024 - 1))
readonly MIN_FREE_BYTES=$((64 * 1024 * 1024 * 1024))
readonly MIN_FREE_INODES=250000
readonly -a RC=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent
  rclone --config "$R2_CONF" --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G)
# `--immutable` below is only a client-side collision guard. Durability comes
# from the independently attested server-side R2 Bucket Locks checked above.

die() { printf 'postiz artifact backup: %s\n' "$*" >&2; exit 1; }
log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }

usage() {
  printf 'usage: %s --timestamp UTC --sealed-upload-root DIR --sealed-upload-manifest FILE --capture-evidence FILE --runtime-config-archive FILE --expected-compose-sha256 SHA --expected-dockerfile-sha256 SHA --receipt-out FILE\n' "$0" >&2
  exit 64
}

timestamp=
receipt_out=
upload_root=
sealed_manifest=
expected_compose_sha=
expected_dockerfile_sha=
capture_evidence=
runtime_config_archive=
while (($#)); do
  case "$1" in
    --timestamp) (($# >= 2)) || usage; timestamp=$2; shift 2 ;;
    --sealed-upload-root) (($# >= 2)) || usage; upload_root=$2; shift 2 ;;
    --sealed-upload-manifest) (($# >= 2)) || usage; sealed_manifest=$2; shift 2 ;;
    --capture-evidence) (($# >= 2)) || usage; capture_evidence=$2; shift 2 ;;
    --runtime-config-archive) (($# >= 2)) || usage; runtime_config_archive=$2; shift 2 ;;
    --expected-compose-sha256) (($# >= 2)) || usage; expected_compose_sha=$2; shift 2 ;;
    --expected-dockerfile-sha256) (($# >= 2)) || usage; expected_dockerfile_sha=$2; shift 2 ;;
    --receipt-out) (($# >= 2)) || usage; receipt_out=$2; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$timestamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || usage
[[ "$receipt_out" == /* && "$receipt_out" != */../* && "$receipt_out" != */./* ]] || usage
[[ "$upload_root" == /* && -d "$upload_root" && ! -L "$upload_root" ]] || usage
[[ "$sealed_manifest" == /* && -f "$sealed_manifest" && ! -L "$sealed_manifest" ]] || usage
[[ "$capture_evidence" == /* && -f "$capture_evidence" && ! -L "$capture_evidence" ]] || usage
[[ "$runtime_config_archive" == /* && -f "$runtime_config_archive" && \
   ! -L "$runtime_config_archive" ]] || usage
[[ "$expected_compose_sha" =~ ^[0-9a-f]{64}$ && \
   "$expected_dockerfile_sha" =~ ^[0-9a-f]{64}$ ]] || usage
(( EUID == 0 )) || die 'must run as root'

safe_root_file() {
  local path=$1 expected_mode=$2 actual
  [[ -f "$path" && ! -L "$path" ]] || die "required regular file is missing: $path"
  actual=$(stat -Lc '%u:%g:%a:%h' -- "$path")
  [[ "$actual" == "0:0:${expected_mode}:1" ]] || die "unsafe owner/mode/link count: $path"
}

safe_root_file "$BACKUP_KEY" 600
safe_root_file "$R2_CONF" 600
safe_root_file "$HELPER" 755
safe_root_file "$POLICY_ATTESTER" 755
safe_root_file "$WORKSPACE_CLEANUP" 755
safe_root_file "$COMPOSE" 644
safe_root_file "$DOCKERFILE" 644
[[ -d "$LIVE_UPLOAD_ROOT" && ! -L "$LIVE_UPLOAD_ROOT" ]] || die 'Postiz uploads volume is unavailable'
[[ "$(docker volume inspect -f '{{.Mountpoint}}' postiz_postiz-uploads)" == "$LIVE_UPLOAD_ROOT" ]] \
  || die 'Postiz uploads volume mountpoint drifted'
command -v flock >/dev/null || die 'flock is missing'
command -v gzip >/dev/null || die 'gzip is missing'
command -v openssl >/dev/null || die 'openssl is missing'
command -v rclone >/dev/null || die 'rclone is missing'
command -v docker >/dev/null || die 'docker is missing'
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || die 'backup StateDirectory is unsafe'
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || die 'backup RuntimeDirectory is unsafe'
safe_root_file "$ARTIFACT_LOCK" 600
"$POLICY_ATTESTER"
safe_root_file "$STORAGE_POLICY" 600
"$HELPER" verify-storage-policy --policy "$STORAGE_POLICY"
"$HELPER" verify-config-source --archive "$runtime_config_archive"

free_bytes=$(df -B1 --output=avail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
free_inodes=$(df --output=iavail "$STATE_ROOT" | tail -1 | tr -d '[:space:]')
[[ "$free_bytes" =~ ^[0-9]+$ && "$free_inodes" =~ ^[0-9]+$ ]] || die 'cannot measure artifact workspace capacity'
((free_bytes >= MIN_FREE_BYTES && free_inodes >= MIN_FREE_INODES)) \
  || die 'artifact workspace byte/inode preflight failed'

exec 9<>"$ARTIFACT_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$ARTIFACT_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/9")" ]] || die 'artifact lock descriptor/path drifted'
flock -n 9 || die 'another Postiz artifact backup is running'
"$WORKSPACE_CLEANUP" --scope artifact --lock-held-fd 9

work=$(mktemp -d "$STATE_ROOT/postiz-artifact.XXXXXX") \
  || die 'cannot create Postiz artifact workspace'
cleanup() { rm -rf --one-file-system -- "$work"; }
trap cleanup EXIT

# Every persistent Docker mount of the four Postiz services must have an explicit
# recovery owner. An added/renamed/bind mount stops backup until coverage is designed.
actual_mounts="$work/actual-mounts.txt"
install -m 600 /dev/null "$actual_mounts"
for service in postiz postiz-postgres postiz-redis postiz-temporal; do
  mount_listing="$work/mounts-$service.txt"
  docker inspect --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}{{println}}{{end}}' \
    "$service" > "$mount_listing"
  while IFS='|' read -r mount_type mount_name destination; do
    [[ -z "$mount_type$mount_name$destination" ]] && continue
    printf '%s|%s|%s|%s\n' "$service" "$mount_type" "$mount_name" "$destination" >> "$actual_mounts"
  done < "$mount_listing"
done
sort -o "$actual_mounts" "$actual_mounts"
expected_mounts='postiz-postgres|volume|postiz_postiz-postgres|/var/lib/postgresql/data
postiz-redis|volume|postiz_postiz-redis|/data
postiz|volume|postiz_postiz-config|/config
postiz|volume|postiz_postiz-uploads|/uploads'
[[ "$(cat "$actual_mounts")" == "$expected_mounts" ]] || die 'Postiz persistent mount coverage drifted'

manifest=$sealed_manifest
blob_list="$work/blob-list.txt"
log 'validating writer-fenced uploads snapshot'
"$HELPER" verify-source --root "$upload_root" --manifest "$manifest" \
  --max-files "$MAX_FILES" --max-bytes "$MAX_SOURCE_BYTES"
"$HELPER" emit-blob-list --manifest "$manifest" --output "$blob_list"
IFS=$'\t' read -r file_count total_bytes < <("$HELPER" summary --manifest "$manifest")
[[ "$file_count" =~ ^[0-9]+$ && "$total_bytes" =~ ^[0-9]+$ ]] || die 'invalid upload totals'

remote_file_size() {
  local remote=$1 key=$2 line size path listing count
  listing="$work/remote-file-$(printf '%s' "$remote/$key" | sha256sum | cut -d' ' -f1).txt"
  "${RC[@]}" lsf "$remote/$(dirname "$key")" --files-only \
    --include "$(basename "$key")" --format sp --separator '|' > "$listing"
  count=$(wc -l < "$listing")
  [[ "$count" =~ ^[0-9]+$ && "$count" -le 1 ]] || die 'remote exact-object listing is ambiguous'
  line=$(head -1 "$listing")
  [[ -n "$line" ]] || return 0
  IFS='|' read -r size path <<< "$line"
  [[ "$size" =~ ^[0-9]+$ && "$path" == "$(basename "$key")" ]] \
    || die 'invalid remote exact-object listing'
  printf '%s\n' "$size"
}

for remote in "$PRIMARY" "$DR"; do
  "${RC[@]}" mkdir "$remote/$BLOB_PREFIX"
done

declare -A primary_size=()
declare -A dr_size=()
load_inventory() {
  local remote=$1 array_name=$2 size key listing
  local -n inventory=$array_name
  listing="$work/inventory-${array_name}.txt"
  "${RC[@]}" lsf "$remote/$BLOB_PREFIX" --recursive --files-only \
    --files-from "$blob_list" --format sp --separator '|' > "$listing"
  while IFS='|' read -r size key; do
    [[ -z "$size$key" ]] && continue
    [[ "$size" =~ ^[0-9]+$ ]] || die "invalid remote object size under $remote"
    [[ "$key" =~ ^[0-9a-f]{2}/[0-9a-f]{64}\.enc$ ]] || die "invalid expected object under $remote"
    [[ -z "${inventory[$key]+x}" ]] || die "duplicate remote object under $remote"
    inventory[$key]=$size
  done < "$listing"
}
load_inventory "$PRIMARY" primary_size
load_inventory "$DR" dr_size

new_spool="$work/new"
from_primary="$work/from-primary"
from_dr="$work/from-dr"
mkdir -m 700 "$new_spool" "$from_primary" "$from_dr"
to_primary="$work/to-primary.txt"
to_dr="$work/to-dr.txt"
install -m 600 /dev/null "$to_primary"
install -m 600 /dev/null "$to_dr"
new_cipher_bytes=0

expected_cipher_size() {
  local plain=$1
  printf '%s\n' $((16 + ((plain / 16) + 1) * 16))
}

verify_cipher() {
  local cipher=$1 expected_sha=$2 expected_size=$3 actual_size actual_sha
  [[ -f "$cipher" && ! -L "$cipher" ]] || die 'cipher blob is missing or unsafe'
  actual_size=$(stat -Lc '%s' -- "$cipher")
  [[ "$actual_size" == "$expected_size" ]] || die 'cipher blob has an unexpected size'
  actual_sha=$(openssl enc -d -aes-256-cbc -pbkdf2 -in "$cipher" \
    -pass file:"$BACKUP_KEY" 2>/dev/null | sha256sum | cut -d' ' -f1)
  [[ "$actual_sha" == "$expected_sha" ]] || die 'cipher blob does not decrypt to its content address'
}

declare -A seen_blob=()
declare -A expected_size_by_key=()
while IFS=$'\t' read -r digest size relative; do
  [[ "$digest" =~ ^[0-9a-f]{64}$ && "$size" =~ ^[0-9]+$ ]] || die 'invalid helper entry stream'
  key="${digest:0:2}/${digest}.enc"
  cipher_size=$(expected_cipher_size "$size")
  if [[ -n "${seen_blob[$key]+x}" ]]; then
    [[ "${expected_size_by_key[$key]}" == "$cipher_size" ]] || die 'digest size collision'
    continue
  fi
  seen_blob[$key]=1
  expected_size_by_key[$key]=$cipher_size
  p_size=${primary_size[$key]:-}
  d_size=${dr_size[$key]:-}
  [[ -z "$p_size" || "$p_size" == "$cipher_size" ]] || die 'primary blob size mismatch'
  [[ -z "$d_size" || "$d_size" == "$cipher_size" ]] || die 'DR blob size mismatch'
  if [[ -n "$p_size" && -n "$d_size" ]]; then
    continue
  fi
  if [[ -n "$p_size" ]]; then
    printf '%s\n' "$key" >> "$to_dr"
    continue
  fi
  if [[ -n "$d_size" ]]; then
    printf '%s\n' "$key" >> "$to_primary"
    continue
  fi
  target="$new_spool/$key"
  mkdir -m 700 -p -- "$(dirname -- "$target")"
  ((new_cipher_bytes + cipher_size <= MAX_NEW_CIPHER_BYTES)) \
    || die 'new upload data exceeds per-run byte ceiling before encryption'
  (
    ulimit -f $(((cipher_size + 1023) / 1024))
    openssl enc -aes-256-cbc -pbkdf2 -salt \
      -in "$upload_root/$relative" -out "$target" -pass file:"$BACKUP_KEY"
  ) || die 'bounded upload encryption failed'
  verify_cipher "$target" "$digest" "$cipher_size"
  new_cipher_bytes=$((new_cipher_bytes + cipher_size))
done < <("$HELPER" entries --manifest "$manifest")

repair_from_remote() {
  local source=$1 destination=$2 include_file=$3 spool=$4 key digest expected
  [[ -s "$include_file" ]] || return 0
  "${RC[@]}" copy "$source/$BLOB_PREFIX" "$spool" --files-from "$include_file" \
    --checksum --max-transfer "$MAX_SOURCE_BYTES" --cutoff-mode hard --transfers 1
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    digest=${key#*/}; digest=${digest%.enc}
    expected=${expected_size_by_key[$key]}
    verify_cipher "$spool/$key" "$digest" "$expected"
  done < "$include_file"
  "${RC[@]}" copy "$spool" "$destination/$BLOB_PREFIX" \
    --files-from "$include_file" --immutable --checksum --transfers 8
}

repair_from_remote "$PRIMARY" "$DR" "$to_dr" "$from_primary"
repair_from_remote "$DR" "$PRIMARY" "$to_primary" "$from_dr"

if [[ -n "$(find "$new_spool" -type f -print -quit)" ]]; then
  "${RC[@]}" copy "$new_spool" "$PRIMARY/$BLOB_PREFIX" --immutable --checksum --transfers 8
  "${RC[@]}" copy "$new_spool" "$DR/$BLOB_PREFIX" --immutable --checksum --transfers 8
fi

verify_remote_blob_set() {
  local remote=$1 label=$2 key digest expected verify_root
  verify_root="$work/verify-$label"
  mkdir -m 700 "$verify_root"
  "${RC[@]}" copy "$remote/$BLOB_PREFIX" "$verify_root" \
    --files-from "$blob_list" --checksum --max-transfer "$MAX_SOURCE_BYTES" \
    --cutoff-mode hard --transfers 1
  "$HELPER" verify-cipher-tree --manifest "$manifest" --root "$verify_root"
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    digest=${key#*/}; digest=${digest%.enc}
    expected=${expected_size_by_key[$key]}
    verify_cipher "$verify_root/$key" "$digest" "$expected"
  done < "$blob_list"
  rm -rf -- "$verify_root"
}

# Existing content-addressed objects are not trusted merely because their key
# and size match.  A delete-capable/append-capable S3 credential could preplay a
# poisoned object before a lock applies.  Stream a bounded current-set copy from
# each remote and prove every ciphertext decrypts to its plaintext address.
# This is at most 2 * MAX_SOURCE_BYTES download and never reuploads unchanged
# upload content.
verify_remote_blob_set "$PRIMARY" primary
verify_remote_blob_set "$DR" dr

"${RC[@]}" check "$PRIMARY/$BLOB_PREFIX" "$DR/$BLOB_PREFIX" \
  --files-from "$blob_list" --one-way --checksum --checkers 16
"$HELPER" verify-source --root "$upload_root" --manifest "$manifest" \
  --max-files "$MAX_FILES" --max-bytes "$MAX_SOURCE_BYTES"

month="${timestamp:0:4}-${timestamp:4:2}"
manifest_key="uploads/manifests/$month/uploads-${timestamp}.json.enc"
manifest_cipher_dir="$work/manifest-cipher"
mkdir -m 700 "$manifest_cipher_dir"
manifest_cipher="$manifest_cipher_dir/$(basename "$manifest_key")"
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$manifest" -out "$manifest_cipher" \
  -pass file:"$BACKUP_KEY"
manifest_plain_sha=$(sha256sum "$manifest" | cut -d' ' -f1)
[[ "$(openssl enc -d -aes-256-cbc -pbkdf2 -in "$manifest_cipher" \
  -pass file:"$BACKUP_KEY" 2>/dev/null | sha256sum | cut -d' ' -f1)" == "$manifest_plain_sha" ]] \
  || die 'upload manifest encryption round-trip failed'
for remote in "$PRIMARY" "$DR"; do
  "${RC[@]}" copyto "$manifest_cipher" "$remote/$manifest_key" --immutable --checksum
  "${RC[@]}" check "$manifest_cipher_dir" "$remote/$(dirname "$manifest_key")" \
    --include "$(basename "$manifest_key")" --one-way --checksum
done
manifest_cipher_sha=$(sha256sum "$manifest_cipher" | cut -d' ' -f1)

fetch_image_cipher() {
  local remote=$1 key=$2 destination=$3 expected_bytes=$4
  [[ "$expected_bytes" =~ ^[0-9]+$ && "$expected_bytes" -gt 0 && \
     "$expected_bytes" -le "$MAX_IMAGE_CIPHER_BYTES" ]] \
    || die 'remote image size preflight is invalid'
  (
    ulimit -f $(((MAX_IMAGE_CIPHER_BYTES + 1023) / 1024))
    timeout --signal=TERM --kill-after=10s 1800s \
      "${RC[@]}" copyto "$remote/$key" "$destination" --checksum
  ) || die 'bounded remote image fetch failed'
  [[ -f "$destination" && ! -L "$destination" && \
     "$(stat -Lc '%s' "$destination")" == "$expected_bytes" ]] \
    || die 'remote image changed across bounded fetch'
}

image_records="$work/image-records"
mkdir -m 700 "$image_records"
total_new_image_bytes=0
for spec in "${IMAGE_SPECS[@]}"; do
  IFS='|' read -r service image_ref <<< "$spec"
  expected_container_id=$("$HELPER" capture-writer-get --evidence "$capture_evidence" \
    --service "$service" --key container_id)
  expected_image_id=$("$HELPER" capture-writer-get --evidence "$capture_evidence" \
    --service "$service" --key image_id)
  image_id=$(docker image inspect --format '{{.Id}}' "$image_ref")
  IFS='|' read -r actual_container_id container_image_id container_running < <(
    docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}' "$service"
  )
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ && "$container_image_id" == "$image_id" && \
     "$actual_container_id|$container_image_id|$container_running" == \
       "$expected_container_id|$expected_image_id|true" ]] \
    || die "running service/image mapping drifted: $service"
  image_hex=${image_id#sha256:}
  image_key="images/sha256/${image_hex}.docker.tar.gz.enc"
  image_sidecar_key="${image_key}.sha256"
  image_dir="$work/image-$service"
  mkdir -m 700 "$image_dir"
  image_cipher="$image_dir/$(basename "$image_key")"
  image_sidecar="$image_dir/$(basename "$image_sidecar_key")"
  image_expanded_stats="$image_dir/uncompressed-bytes.txt"
  image_inode_stats="$image_dir/uncompressed-inodes.txt"
  for remote in "$PRIMARY" "$DR"; do
    "${RC[@]}" mkdir "$remote/$(dirname "$image_key")"
  done

  p_image_size=$(remote_file_size "$PRIMARY" "$image_key")
  d_image_size=$(remote_file_size "$DR" "$image_key")
  p_sidecar_size=$(remote_file_size "$PRIMARY" "$image_sidecar_key")
  d_sidecar_size=$(remote_file_size "$DR" "$image_sidecar_key")

  verify_image_cipher() {
    local cipher=$1 plain=$2 byte_stats=$3 inode_stats=$4
    local cipher_bytes
    cipher_bytes=$(stat -Lc '%s' "$cipher")
    ((cipher_bytes > 0 && cipher_bytes <= MAX_IMAGE_CIPHER_BYTES)) \
      || die 'service image archive exceeds byte ceiling'
    (
      ulimit -f $(((cipher_bytes + 1023) / 1024))
      timeout --signal=TERM --kill-after=10s 1800s \
        openssl enc -d -aes-256-cbc -pbkdf2 -in "$cipher" -out "$plain" \
          -pass file:"$BACKUP_KEY"
    ) || die 'bounded Docker image archive decryption failed'
    "$HELPER" verify-image-archive --archive "$plain" --image-id "$image_id" \
      --uncompressed-bytes-output "$byte_stats" \
      --uncompressed-inodes-output "$inode_stats"
    rm -f -- "$plain"
  }

  if [[ -n "$p_image_size" ]]; then
    ((p_image_size <= MAX_IMAGE_CIPHER_BYTES)) || die 'primary image archive exceeds byte ceiling'
    fetch_image_cipher "$PRIMARY" "$image_key" "$image_cipher" "$p_image_size"
    verify_image_cipher "$image_cipher" "$work/$service-primary.docker.tar.gz" \
      "$image_expanded_stats" "$image_inode_stats"
  elif [[ -n "$d_image_size" ]]; then
    ((d_image_size <= MAX_IMAGE_CIPHER_BYTES)) || die 'DR image archive exceeds byte ceiling'
    fetch_image_cipher "$DR" "$image_key" "$image_cipher" "$d_image_size"
    verify_image_cipher "$image_cipher" "$work/$service-dr.docker.tar.gz" \
      "$image_expanded_stats" "$image_inode_stats"
  else
    log "creating one-time content-addressed image archive: $service"
    (
      ulimit -f $((MAX_IMAGE_CIPHER_BYTES / 1024))
      set -o pipefail
      docker image save "$image_id" | gzip -1 | \
        openssl enc -aes-256-cbc -pbkdf2 -salt -out "$image_cipher" \
          -pass file:"$BACKUP_KEY"
    ) || die 'bounded Docker image archive creation failed'
    total_new_image_bytes=$((total_new_image_bytes + $(stat -Lc '%s' "$image_cipher")))
    ((total_new_image_bytes <= 12 * 1024 * 1024 * 1024)) \
      || die 'new image archives exceed aggregate per-run byte ceiling'
    verify_image_cipher "$image_cipher" "$work/$service-new.docker.tar.gz" \
      "$image_expanded_stats" "$image_inode_stats"
  fi

  image_cipher_bytes=$(stat -Lc '%s' "$image_cipher")
  image_cipher_sha=$(sha256sum "$image_cipher" | cut -d' ' -f1)
  image_uncompressed_bytes=$(tr -d '[:space:]' < "$image_expanded_stats")
  image_uncompressed_inodes=$(tr -d '[:space:]' < "$image_inode_stats")
  [[ "$image_cipher_sha" =~ ^[0-9a-f]{64}$ && \
     "$image_uncompressed_bytes" =~ ^[0-9]+$ && \
     "$image_uncompressed_inodes" =~ ^[0-9]+$ ]] \
    || die 'invalid verified service-image statistics'

  # If both remotes already carry the image, independently decrypt and verify
  # the second copy too.  Random-salt ciphertexts are not interchangeable: the
  # committed receipt binds one exact ciphertext on both locked remotes.
  if [[ -n "$p_image_size" && -n "$d_image_size" ]]; then
    second_cipher="$image_dir/dr-existing.docker.tar.gz.enc"
    second_bytes="$image_dir/dr-existing.bytes"
    second_inodes="$image_dir/dr-existing.inodes"
    fetch_image_cipher "$DR" "$image_key" "$second_cipher" "$d_image_size"
    verify_image_cipher "$second_cipher" "$work/$service-dr-second.docker.tar.gz" \
      "$second_bytes" "$second_inodes"
    [[ "$(sha256sum "$second_cipher" | cut -d' ' -f1)" == "$image_cipher_sha" && \
       "$(tr -d '[:space:]' < "$second_bytes")" == "$image_uncompressed_bytes" && \
       "$(tr -d '[:space:]' < "$second_inodes")" == "$image_uncompressed_inodes" ]] \
      || die 'primary and DR image ciphertexts are not the same verified object'
    rm -f -- "$second_cipher" "$second_bytes" "$second_inodes"
  fi
  ((image_cipher_bytes > 0 && image_cipher_bytes <= MAX_IMAGE_CIPHER_BYTES)) \
    || die 'service image archive exceeds byte ceiling'
  [[ -z "$p_image_size" || "$p_image_size" == "$image_cipher_bytes" ]] \
    || die 'primary image archive size mismatch'
  [[ -z "$d_image_size" || "$d_image_size" == "$image_cipher_bytes" ]] \
    || die 'DR image archive size mismatch'

  printf '%s %s %s %s\n' "$image_cipher_sha" "$image_uncompressed_bytes" \
    "$image_uncompressed_inodes" "$(basename "$image_key")" > "$image_sidecar"
  chmod 600 "$image_sidecar"
  for remote in "$PRIMARY" "$DR"; do
    "${RC[@]}" copyto "$image_cipher" "$remote/$image_key" --immutable --checksum
    "${RC[@]}" copyto "$image_sidecar" "$remote/$image_sidecar_key" --immutable --checksum
    "${RC[@]}" check "$image_dir" "$remote/$(dirname "$image_key")" \
      --include "$(basename "$image_key")" --include "$(basename "$image_sidecar_key")" \
      --one-way --checksum
  done
  "$HELPER" write-image-record \
    --service "$service" \
    --configured-ref "$image_ref" \
    --image-id "$image_id" \
    --archive-key "$image_key" \
    --archive-cipher-sha256 "$image_cipher_sha" \
    --archive-cipher-bytes "$image_cipher_bytes" \
    --archive-uncompressed-bytes "$image_uncompressed_bytes" \
    --archive-uncompressed-inodes "$image_uncompressed_inodes" \
    --output "$image_records/$service.json"
  rm -rf -- "$image_dir"
done

compose_sha=$(sha256sum "$COMPOSE" | cut -d' ' -f1)
dockerfile_sha=$(sha256sum "$DOCKERFILE" | cut -d' ' -f1)
[[ "$compose_sha" == "$expected_compose_sha" && \
   "$dockerfile_sha" == "$expected_dockerfile_sha" ]] \
  || die 'live runtime config differs from the writer-fenced archive'
[[ "$(sha256sum "$COMPOSE" | cut -d' ' -f1)" == "$compose_sha" && \
   "$(sha256sum "$DOCKERFILE" | cut -d' ' -f1)" == "$dockerfile_sha" ]] \
  || die 'runtime config changed while creating the artifact receipt'
"$HELPER" verify-config-source --archive "$runtime_config_archive"
"$HELPER" write-artifact-receipt \
  --timestamp "$timestamp" \
  --upload-manifest "$manifest" \
  --upload-manifest-key "$manifest_key" \
  --upload-manifest-cipher-sha256 "$manifest_cipher_sha" \
  --image-record-dir "$image_records" \
  --compose-sha256 "$compose_sha" \
  --dockerfile-sha256 "$dockerfile_sha" \
  --output "$receipt_out"
chmod 600 "$receipt_out"
log "Postiz artifacts committed to primary+DR (${file_count} files, ${total_bytes} bytes; new cipher ${new_cipher_bytes} bytes)"
