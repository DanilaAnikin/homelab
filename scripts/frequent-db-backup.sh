#!/usr/bin/env bash
# ============================================================================
# frequent-db-backup.sh — časté ŠIFROVANÉ dumpy prod DB á 10 min → R2 (RPO ~10 min).
# Lehčí než nightly (jen DB, žádný config/secrets bundle). Krátká retence 48h.
# Doplněk nočních záloh (ty drží 30 dní + config + secrets).
# ============================================================================
set -uo pipefail
BACKUP_KEY="/srv/homelab/secrets/freio-backup-key.txt"
R2_REMOTE="r2:homelab-backups"
R2_CONF="/srv/homelab/secrets/rclone.conf"
PREFIX="frequent/$(date +%Y-%m-%d)"
KEEP_HOURS=48
TS=$(date +%Y%m%dT%H%M%SZ)
WORK=$(mktemp -d /tmp/hbk.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
RC="rclone --config $R2_CONF --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G"
FAIL=0

# container_prefix|user|"db1 db2"
TARGETS=(
  "shared-postgres|postgres|lokwave inngest"
  "ripieno-postgres|postgres|ripieno"
  "launchmail-postgres|postgres|launchmail"
  "supabase-db|postgres|freio"
  "classio-supabase-db|supabase_admin|postgres"
  "gorillatype-supabase-db|supabase_admin|postgres"
)
resolve(){ docker ps --format '{{.Names}}' | grep -E "^$1" | head -1; }
[[ -s "$BACKUP_KEY" ]] || { echo "chybí klíč"; exit 1; }

for t in "${TARGETS[@]}"; do
  IFS='|' read -r pfx user dbs <<< "$t"
  c=$(resolve "$pfx"); [[ -z "$c" ]] && { echo "!! $pfx neběží"; FAIL=1; continue; }
  for db in $dbs; do
    if docker exec "$c" pg_dump -U "$user" -Fc -Z6 "$db" > "$WORK/db_${pfx}_${db}_$TS.dump" 2>/dev/null && [[ -s "$WORK/db_${pfx}_${db}_$TS.dump" ]]; then
      openssl enc -aes-256-cbc -pbkdf2 -salt -in "$WORK/db_${pfx}_${db}_$TS.dump" -out "$WORK/db_${pfx}_${db}_$TS.dump.enc" -pass file:"$BACKUP_KEY" && rm -f "$WORK/db_${pfx}_${db}_$TS.dump"
    else echo "!! dump $pfx/$db selhal"; FAIL=1; rm -f "$WORK/db_${pfx}_${db}_$TS.dump"; fi
  done
done

N=$(ls "$WORK"/*.enc 2>/dev/null | wc -l)
if [[ "$N" -gt 0 ]] && $RC copy "$WORK" "$R2_REMOTE/$PREFIX/" --include '*.enc' --transfers 4 -q; then
  $RC delete "$R2_REMOTE/frequent" --min-age "${KEEP_HOURS}h" -q || true
  echo "✔ frequent OK ($N DB, $TS)"
  [[ $FAIL -eq 0 ]] && exit 0 || exit 1
else
  echo "✘ frequent upload/dump selhal"; exit 1
fi
