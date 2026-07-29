#!/usr/bin/env bash
# ============================================================================
# restore-drill.sh — týdenní ověření, že šifrované zálohy JSOU obnovitelné.
# „Netestovaný dump není záloha." Stáhne nejnovější .enc z R2, dešifruje, obnoví
# do IZOLOVANÉHO throwaway postgresu, ověří schema + data (řádky), uklidí, hlásí
# na Telegram. NIKDY nesahá na produkční DB/volumes — vlastní dočasný kontejner.
# ============================================================================
set -uo pipefail
BACKUP_KEY=/srv/homelab/secrets/freio-backup-key.txt
R2CONF=/srv/homelab/secrets/rclone.conf
NIGHTLY="r2:homelab-backups/nightly/$(date +%Y-%m)"
DRILL_IMG="supabase/postgres:17.6.1.136"   # má supabase extenze (freio) i vanilla dumpy zvládne
DRILL_CT="pg-restore-drill"
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/restore-drill.log
DBS="freio ripieno lokwave"

WORK=$(mktemp -d)
cleanup(){ docker rm -f "$DRILL_CT" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

log(){ echo "[$(date +%H:%M:%S)] $*"; }
RESULTS=""; FAIL=0

[[ -s "$BACKUP_KEY" ]] || { echo "chybí klíč"; exit 1; }

# ── throwaway postgres (izolovaný, žádná prod síť, žádný host port) ──────────
docker rm -f "$DRILL_CT" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_CT" -e POSTGRES_PASSWORD=drill "$DRILL_IMG" >/dev/null
log "throwaway postgres startuje…"
ready=0
for _ in $(seq 1 45); do
  if docker exec "$DRILL_CT" pg_isready -U postgres >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[[ $ready -eq 1 ]] || { $NOTIFY "🔄 Restore drill ❌ throwaway postgres nenaběhl"; exit 1; }
sleep 3   # doběhnutí supabase init (role/extenze)

# ── per-DB: stáhni → dešifruj → restore → ověř ──────────────────────────────
for db in $DBS; do
  f=$(rclone --config "$R2CONF" lsf "$NIGHTLY/" 2>/dev/null | grep "^db_${db}_" | sort | tail -1)
  if [[ -z "$f" ]]; then RESULTS+="❌ ${db}: žádná záloha na R2\n"; FAIL=1; continue; fi

  if ! rclone --config "$R2CONF" copyto "$NIGHTLY/$f" "$WORK/$db.enc" -q; then
    RESULTS+="❌ ${db}: stažení selhalo\n"; FAIL=1; continue; fi
  if ! openssl enc -d -aes-256-cbc -pbkdf2 -in "$WORK/$db.enc" -out "$WORK/$db.dump" -pass file:"$BACKUP_KEY" 2>/dev/null; then
    RESULTS+="❌ ${db}: DEŠIFROVÁNÍ selhalo (klíč?)\n"; FAIL=1; continue; fi

  docker exec "$DRILL_CT" psql -U postgres -q -c "DROP DATABASE IF EXISTS d_$db" >/dev/null 2>&1
  docker exec "$DRILL_CT" psql -U postgres -q -c "CREATE DATABASE d_$db" >/dev/null 2>&1
  docker cp "$WORK/$db.dump" "$DRILL_CT:/tmp/$db.dump" >/dev/null 2>&1
  # pg_restore smí mít nefatální chyby (chybějící role/grant) — úspěch měříme daty, ne exit kódem
  docker exec "$DRILL_CT" pg_restore --no-owner --no-privileges -U postgres -d "d_$db" "/tmp/$db.dump" >/dev/null 2>&1 || true
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
  rm -f "$WORK/$db.enc" "$WORK/$db.dump"
done

MSG=$(echo -e "$RESULTS")
{ echo "═══ RESTORE DRILL $(date -Iseconds) ═══"; echo -e "$RESULTS"; } >> "$LOG"
if [[ $FAIL -eq 0 ]]; then
  $NOTIFY "🔄 Restore drill ✅ všechny zálohy obnovitelné:"$'\n'"$MSG" || true
  exit 0
else
  $NOTIFY "🔄 Restore drill ❌ PROBLÉM s obnovitelností:"$'\n'"$MSG" || true
  exit 1
fi
