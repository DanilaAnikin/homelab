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
# Whole-operation retries absorb transient R2/S3 edge failures; low-level
# retries cover individual HTTP transport failures.
RC="rclone --config $R2_CONF --retries 8 --retries-sleep 20s --low-level-retries 10 --s3-upload-cutoff 5G"
# Stav poslední ÚSPĚŠNÉ zálohy — staleness-gate proti flappy alertům při krátkém výpadku R2.
STATE="/var/lib/homelab/frequent-backup-last-ok"
STALE_SEC=2700   # 45 min: teprve pak je propadlá záloha skutečný problém (RPO worst-case)
mkdir -p "$(dirname "$STATE")"
FAIL=0

# container_prefix|user|"db1 db2"
TARGETS=(
  "shared-postgres|postgres|lokwave inngest"
  "ripieno-postgres|postgres|ripieno"
  "launchmail-postgres|postgres|launchmail"
  "supabase-db|postgres|freio"
  "classio-supabase-db|supabase_admin|postgres"
  "natetrader-supabase-db|supabase_admin|postgres"
  "loot-supabase-db|supabase_admin|postgres"
  "contentgen-postgres|contentgen|contentgen"
  "lifeadmin-supabase-db|supabase_admin|postgres"
  "hummy-supabase-db|supabase_admin|postgres"
  "explainact-supabase-db|supabase_admin|postgres"
  "gorillatype-supabase-db|supabase_admin|postgres"
)
resolve(){ docker ps --format '{{.Names}}' | grep -E "^$1" | head -1; }
[[ -s "$BACKUP_KEY" ]] || { echo "chybí klíč"; exit 1; }

for t in "${TARGETS[@]}"; do
  IFS='|' read -r pfx user dbs <<< "$t"
  c=$(resolve "$pfx"); [[ -z "$c" ]] && { echo "-- $pfx neběží — přeskakuji (pauznutá appka, nezálohuje se)"; continue; }
  for db in $dbs; do
    if docker exec "$c" pg_dump -U "$user" -Fc -Z6 "$db" > "$WORK/db_${pfx}_${db}_$TS.dump" 2>/dev/null && [[ -s "$WORK/db_${pfx}_${db}_$TS.dump" ]]; then
      openssl enc -aes-256-cbc -pbkdf2 -salt -in "$WORK/db_${pfx}_${db}_$TS.dump" -out "$WORK/db_${pfx}_${db}_$TS.dump.enc" -pass file:"$BACKUP_KEY" && rm -f "$WORK/db_${pfx}_${db}_$TS.dump"
    else echo "!! dump $pfx/$db selhal"; FAIL=1; rm -f "$WORK/db_${pfx}_${db}_$TS.dump"; fi
  done
done

N=$(ls "$WORK"/*.enc 2>/dev/null | wc -l)
if [[ "$N" -gt 0 ]] && $RC copy "$WORK" "$R2_REMOTE/$PREFIX/" --include '*.enc' --transfers 4 -q; then
  $RC delete "$R2_REMOTE/frequent" --min-age "${KEEP_HOURS}h" -q || true
  date +%s > "$STATE"
  echo "✔ frequent OK ($N DB, $TS)"
  # Dump-chyba některé DB je vždy skutečný problém → eskaluj hned.
  [[ $FAIL -eq 0 ]] && exit 0 || { echo "✘ některý pg_dump selhal"; exit 1; }
else
  echo "✘ frequent upload na R2 selhal"
  # Staleness-gate: přechodný blip R2 (poslední úspěch < 45 min) → tiše, další běh to dožene.
  last=$(cat "$STATE" 2>/dev/null || echo 0); now=$(date +%s); age=$(( now - last ))
  if (( age > STALE_SEC )); then
    echo "✘✘ žádná úspěšná záloha už ${age}s (> ${STALE_SEC}s) → ESKALUJI"
    exit 1
  fi
  echo "⚠ přechodný výpadek R2 — poslední úspěch před $(( age/60 )) min, nechávám na další běh (bez alertu)"
  exit 0
fi
