#!/usr/bin/env bash
# ============================================================================
# homelab-backup.sh — denní záloha (spouští systemd timer, viz backup.timer)
#
# Zálohuje: 1) globals (role+hesla), 2) každou DB zvlášť (pg_dump -Fc),
#           3) /etc/dokploy. Odváží na: USB SSD (/mnt/backup) + Cloudflare R2.
# Úspěch hlásí do Uptime Kuma (push monitor) — když ping nepřijde, dostaneš alert.
# ============================================================================
set -uo pipefail   # bez -e: chceme doběhnout všechno a pak reportovat

# ── Konfigurace ─────────────────────────────────────────────────────────────
PG_CONTAINER="shared-postgres"
LOCAL_DIR="/mnt/backup/homelab"        # USB SSD
R2_REMOTE="r2:homelab-backups"         # rclone remote:bucket
KEEP_LOCAL_DAYS=14
KEEP_R2_DAYS=30
KUMA_PUSH_URL=""                       # ← vlož Push URL z Uptime Kuma

TS=$(date +%F_%H-%M)
WORK=$(mktemp -d /tmp/pgbackup.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
FAIL=0

echo "── Homelab záloha $TS ──"

# 1) Globals: role a jejich hesla (bez nich jsou dumpy k ničemu)
docker exec "$PG_CONTAINER" pg_dumpall -U postgres --globals-only \
  | gzip > "$WORK/globals_$TS.sql.gz" || { echo "!! globals selhaly"; FAIL=1; }

# 2) Každá databáze zvlášť → jde obnovit jeden projekt bez ostatních
DBS=$(docker exec "$PG_CONTAINER" psql -U postgres -Atc \
  "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")
for db in $DBS; do
  echo "   pg_dump $db"
  docker exec "$PG_CONTAINER" pg_dump -U postgres -Fc -Z6 "$db" \
    > "$WORK/db_${db}_$TS.dump" || { echo "!! selhal dump $db"; FAIL=1; }
done

# 3) Dokploy konfigurace (definice aplikací, domén, env vars)
tar -czf "$WORK/etc-dokploy_$TS.tar.gz" -C / etc/dokploy 2>/dev/null \
  || { echo "!! tar /etc/dokploy selhal"; FAIL=1; }

# 4) Odvoz — USB (pokud připojené) + R2; stačí, když uspěje aspoň jedno
OK_LOCAL=0; OK_R2=0
if mountpoint -q /mnt/backup; then
  mkdir -p "$LOCAL_DIR" && cp "$WORK"/* "$LOCAL_DIR/" && OK_LOCAL=1
  find "$LOCAL_DIR" -type f -mtime +"$KEEP_LOCAL_DAYS" -delete
else
  echo "!! USB /mnt/backup není připojené — jedu jen do R2"
fi
if rclone copy "$WORK" "$R2_REMOTE/$(date +%Y-%m)/" --transfers 4 -q; then
  OK_R2=1
  rclone delete "$R2_REMOTE" --min-age "${KEEP_R2_DAYS}d" -q || true
else
  echo "!! upload do R2 selhal"
fi
[[ $OK_LOCAL -eq 0 && $OK_R2 -eq 0 ]] && FAIL=1

# 5) Report
if [[ $FAIL -eq 0 ]]; then
  echo "✔ Záloha OK (usb=$OK_LOCAL r2=$OK_R2, $(ls "$WORK" | wc -l) souborů)"
  [[ -n "$KUMA_PUSH_URL" ]] && curl -fsS -m 10 "$KUMA_PUSH_URL?status=up&msg=OK" >/dev/null
  exit 0
else
  echo "✘ ZÁLOHA SELHALA (usb=$OK_LOCAL r2=$OK_R2) — viz log výše"
  exit 1
fi
