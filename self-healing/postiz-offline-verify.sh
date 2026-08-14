#!/bin/sh
# Extract and verify Postiz file state inside a read-only, network-none container.
set -eu
umask 077

fail() {
  printf 'offline Postiz file verification failed: %s\n' "$1" >&2
  exit 1
}

[ "$(ls /sys/class/net 2>/dev/null)" = lo ] || fail 'container has a non-loopback interface'
awk 'NR > 1 { exit 1 }' /proc/net/route || fail 'container has a network route'
case "${EXPECTED_FILE_COUNT:-}" in *[!0-9]*|'') fail 'invalid expected upload count' ;; esac
case "${EXPECTED_TOTAL_BYTES:-}" in *[!0-9]*|'') fail 'invalid expected upload bytes' ;; esac
case "${OPERATOR_STATE_PRESENT:-}" in 0|1) ;; *) fail 'invalid operator-state status' ;; esac

expected_config='etc/homelab/postiz-backup-source-revision
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
srv/homelab/self-healing/postiz-offline-verify.sh
srv/homelab/self-healing/postiz-restore-drill.sh
srv/homelab/self-healing/restore-drill.sh
srv/postiz/Dockerfile.patch
srv/postiz/docker-compose.yml
srv/postiz/postiz.env
srv/postiz/schedule-week.py
usr/local/bin/frequent-db-backup.sh
usr/local/bin/homelab-backup.sh
usr/local/libexec/postiz-backup-manifest.py
usr/local/sbin/postiz-artifact-backup.sh
usr/local/sbin/postiz-backup-workspace-cleanup.sh
usr/local/sbin/postiz-compose-locked.sh
usr/local/sbin/postiz-quiesced-capture.sh
usr/local/sbin/postiz-r2-policy-attest.sh'
actual_config=$(tar -tzf /drill/runtime-config.tar.gz | sed 's#^\./##' | sort)
[ "$actual_config" = "$expected_config" ] || fail 'runtime config allowlist differs'

mkdir -p /drill-output/runtime /drill-output/config-volume
tar --no-same-owner -xzf /drill/runtime-config.tar.gz -C /drill-output/runtime
tar --no-same-owner -xzf /drill/config-volume.tar.gz -C /drill-output/config-volume
[ "$(wc -c < /drill-output/runtime/etc/homelab/postiz-backup-source-revision | tr -d '[:space:]')" = 41 ] \
  && grep -Eq '^[0-9a-f]{40}$' \
    /drill-output/runtime/etc/homelab/postiz-backup-source-revision \
  || fail 'restored tooling source revision is invalid'
[ "$(stat -c '%a:%u:%g' /drill-output/runtime/srv/postiz/postiz.env)" = '600:0:0' ] \
  || fail 'restored postiz.env owner/mode differs'
for name in Dockerfile.patch docker-compose.yml schedule-week.py; do
  metadata=$(stat -c '%u:%g:%A' "/drill-output/runtime/srv/postiz/$name")
  case "$metadata" in
    0:0:?????w????|0:0:????????w?) fail 'restored runtime config is group/other writable' ;;
    0:0:*) ;;
    *) fail 'restored runtime config owner differs' ;;
  esac
done
for name in \
    srv/postiz/schedule-week.py \
    srv/homelab/self-healing/postiz-offline-verify.sh \
    srv/homelab/self-healing/postiz-restore-drill.sh \
    srv/homelab/self-healing/restore-drill.sh \
    usr/local/bin/frequent-db-backup.sh \
    usr/local/bin/homelab-backup.sh \
    usr/local/libexec/postiz-backup-manifest.py \
    usr/local/sbin/postiz-artifact-backup.sh \
    usr/local/sbin/postiz-backup-workspace-cleanup.sh \
    usr/local/sbin/postiz-compose-locked.sh \
    usr/local/sbin/postiz-quiesced-capture.sh \
    usr/local/sbin/postiz-r2-policy-attest.sh; do
  metadata=$(stat -c '%a:%u:%g' "/drill-output/runtime/$name")
  case "$metadata" in 750:0:0|755:0:0) ;; *) fail 'restored recovery tooling mode differs' ;; esac
done

# Parse key names and empty/non-empty state only. Values never reach stdout/stderr.
awk -F= '
  /^[[:space:]]*($|#)/ { next }
  $0 !~ /^[A-Za-z_][A-Za-z0-9_]*=/ { exit 1 }
  {
    key=$1
    if (seen[key]++) exit 1
    if (length(substr($0, index($0, "=") + 1)) == 0) exit 1
  }
  END {
    required["DATABASE_URL"]=1
    required["JWT_SECRET"]=1
    required["REDIS_URL"]=1
    required["MAIN_URL"]=1
    required["FRONTEND_URL"]=1
    required["BACKEND_INTERNAL_URL"]=1
    required["UPLOAD_DIRECTORY"]=1
    required["STORAGE_PROVIDER"]=1
    required["TEMPORAL_ADDRESS"]=1
    for (key in required) if (!seen[key]) exit 1
  }
' /drill-output/runtime/srv/postiz/postiz.env \
  || fail 'postiz.env syntax or required-key contract differs'

actual_count=$(find /drill/uploads -type f | wc -l | tr -d '[:space:]')
[ "$actual_count" = "$EXPECTED_FILE_COUNT" ] || fail 'restored upload file count differs'
actual_bytes=$(find /drill/uploads -type f -exec stat -c '%s' {} \; | \
  awk '{ total += $1 } END { print total + 0 }')
[ "$actual_bytes" = "$EXPECTED_TOTAL_BYTES" ] || fail 'restored upload byte count differs'
(cd /drill/uploads && sha256sum -c /drill/checksums.txt >/dev/null) \
  || fail 'restored upload checksum differs'

if [ "$OPERATOR_STATE_PRESENT" = 1 ]; then
  [ -s /drill/seasonal-policy.json ] || fail 'required seasonal policy is absent'
  mkdir -p /drill-output/seasonal-releases /drill-output/seasonal-anchor-replacement
  tar --no-same-owner -xzf /drill/seasonal-releases.tar.gz \
    -C /drill-output/seasonal-releases
  tar --no-same-owner -xzf /drill/seasonal-anchor-replacement.tar.gz \
    -C /drill-output/seasonal-anchor-replacement
  cp /drill/seasonal-policy.json /drill-output/seasonal-backup-policy.json
  chmod 600 /drill-output/seasonal-backup-policy.json
else
  [ ! -e /drill/seasonal-policy.json ] || fail 'absent seasonal state supplied a policy'
  [ ! -e /drill/seasonal-releases.tar.gz ] || fail 'absent seasonal state supplied releases'
  [ ! -e /drill/seasonal-anchor-replacement.tar.gz ] \
    || fail 'absent seasonal state supplied replacement state'
fi

printf 'offline Postiz config/config-volume/uploads/operator-state restore OK (%s files, %s bytes)\n' \
  "$EXPECTED_FILE_COUNT" "$EXPECTED_TOTAL_BYTES"
