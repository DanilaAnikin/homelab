#!/usr/bin/env bash
# ============================================================================
# rebuild-drill.sh — full DR verifikace: dokazuje, že homelab JDE postavit znovu
# z gitu + R2 + off-box klíče. Nesahá na produkci (throwaway kontejner, temp dir).
# 1) git clone repa (source of truth + pushnuto)  2) fetch nejnovější nightly bundle z R2
# 3) dešifruj VŠE off-box klíčem  4) restore VŠECH DB do throwaway + ověř  5) ověř
# config+secrets bundle  → Telegram verdikt. Doplněk restore-drill (ten jen 3 DB).
# ============================================================================
set -uo pipefail
BACKUP_KEY=/srv/homelab/secrets/freio-backup-key.txt
R2CONF=/srv/homelab/secrets/rclone.conf
RC="rclone --config $R2CONF"
IMG=supabase/postgres:17.6.1.136
CT=rebuild-drill-pg
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/rebuild-drill.log
REPO=https://github.com/DanilaAnikin/homelab
WORK=$(mktemp -d /tmp/rebuild.XXXXXX)
cleanup(){ docker rm -f "$CT" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
R=""; FAIL=0
add(){ R+="$1"$'\n'; }
notify(){ printf '%s\n' "$1" | "$NOTIFY"; }
{ echo "═══ REBUILD DRILL $(date -Iseconds) ═══"; } >> "$LOG"

[[ -s "$BACKUP_KEY" ]] || { notify "🧱 Rebuild drill ❌ chybí off-box klíč"; exit 1; }

# ── 1) git clone (source of truth) ───────────────────────────────────────────
if git clone --depth 1 -q "$REPO" "$WORK/repo" 2>/dev/null && [[ -s "$WORK/repo/scripts/backup.sh" ]]; then
  add "✅ git clone OK ($(find "$WORK/repo" -type f | wc -l) souborů, scripts/backup.sh přítomen)"
else add "❌ git clone / repo neúplný"; FAIL=1; fi

# ── 2) fetch nejnovější nightly bundle z R2 ──────────────────────────────────
MONTH=$(date +%Y-%m)
LATEST=$($RC lsf "r2:homelab-backups/nightly/$MONTH/" 2>/dev/null | grep -oE '[0-9]{8}T[0-9]{6}Z' | sort -u | tail -1)
if [[ -z "$LATEST" ]]; then
  # zkus předchozí měsíc
  MONTH=$(date -d 'last month' +%Y-%m 2>/dev/null || echo "$MONTH")
  LATEST=$($RC lsf "r2:homelab-backups/nightly/$MONTH/" 2>/dev/null | grep -oE '[0-9]{8}T[0-9]{6}Z' | sort -u | tail -1)
fi
[[ -z "$LATEST" ]] && { add "❌ žádný nightly bundle na R2"; FAIL=1; }
if [[ -n "$LATEST" ]]; then
  add "📦 nejnovější nightly: $LATEST"
  $RC copy "r2:homelab-backups/nightly/$MONTH/" "$WORK/enc/" --include "*$LATEST*" -q 2>/dev/null
  N=$(ls "$WORK/enc/" 2>/dev/null | wc -l)
  add "✅ staženo $N .enc souborů"

  # ── 3) dešifruj vše ────────────────────────────────────────────────────────
  mkdir -p "$WORK/dec"; DECOK=0; DECFAIL=0
  for f in "$WORK/enc"/*.enc; do
    out="$WORK/dec/$(basename "${f%.enc}")"
    if openssl enc -d -aes-256-cbc -pbkdf2 -in "$f" -out "$out" -pass file:"$BACKUP_KEY" 2>/dev/null && [[ -s "$out" ]]; then DECOK=$((DECOK+1)); else DECFAIL=$((DECFAIL+1)); fi
  done
  if [[ "$DECFAIL" -eq 0 ]]; then add "✅ dešifrováno $DECOK/$N (off-box klíč OK)"; else add "❌ dešifrování selhalo u $DECFAIL"; FAIL=1; fi

  # ── 4) restore VŠECH DB do throwaway + ověř ──────────────────────────────────
  docker rm -f "$CT" >/dev/null 2>&1 || true
  docker run -d --name "$CT" -e POSTGRES_PASSWORD=drill "$IMG" >/dev/null 2>&1
  ready=0; for _ in $(seq 1 45); do docker exec "$CT" pg_isready -U postgres >/dev/null 2>&1 && { ready=1; break; }; sleep 2; done
  if [[ "$ready" -eq 1 ]]; then
    sleep 3; DBOK=0; DBFAIL=0
    for dump in "$WORK/dec"/db_*.dump; do
      [[ -e "$dump" ]] || continue
      db="restore_$(basename "$dump" | md5sum | cut -c1-10)"
      docker exec "$CT" psql -U postgres -q -c "CREATE DATABASE $db" >/dev/null 2>&1
      docker cp "$dump" "$CT:/tmp/d.dump" >/dev/null 2>&1
      docker exec "$CT" pg_restore --no-owner --no-privileges -U postgres -d "$db" /tmp/d.dump >/dev/null 2>&1 || true
      docker exec "$CT" psql -U postgres -d "$db" -q -c "ANALYZE" >/dev/null 2>&1
      ntab=$(docker exec "$CT" psql -U postgres -d "$db" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'" 2>/dev/null | tr -d '[:space:]')
      if [[ "$ntab" =~ ^[0-9]+$ && "$ntab" -gt 0 ]]; then DBOK=$((DBOK+1)); else DBFAIL=$((DBFAIL+1)); add "  ⚠️ $(basename "$dump"): $ntab tab."; fi
    done
    if [[ "$DBFAIL" -eq 0 && "$DBOK" -gt 0 ]]; then add "✅ obnoveno $DBOK DB (všechny se schématem)"; else add "❌ restore DB: OK=$DBOK FAIL=$DBFAIL"; FAIL=1; fi
  else add "❌ throwaway postgres nenaběhl"; FAIL=1; fi

  # ── 5) ověř config + secrets bundle ──────────────────────────────────────────
  cfg=$(ls "$WORK/dec"/config_*.tar.gz 2>/dev/null | head -1)
  # POZOR: `tar|grep -q` + pipefail = false negativ (grep -q ukončí rouru → tar SIGPIPE).
  # Vylistuj do souboru, pak grepuj bez roury.
  [[ -n "$cfg" ]] && tar tzf "$cfg" > "$WORK/cfg.list" 2>/dev/null
  if [[ -s "$WORK/cfg.list" ]] && grep -q 'srv/homelab/compose' "$WORK/cfg.list" && grep -q 'self-healing' "$WORK/cfg.list"; then
    add "✅ config bundle OK ($(wc -l < "$WORK/cfg.list") položek, compose+self-healing přítomny)"
  else add "❌ config bundle chybí/neúplný"; FAIL=1; fi
  sec=$(ls "$WORK/dec"/secrets_*.tar.gz 2>/dev/null | head -1)
  if [[ -n "$sec" ]] && [[ $(tar tzf "$sec" 2>/dev/null | wc -l) -gt 5 ]]; then
    add "✅ secrets bundle OK ($(tar tzf "$sec" 2>/dev/null | grep -c 'secrets/') souborů)"
  else add "❌ secrets bundle chybí/prázdný"; FAIL=1; fi
fi

MSG=$(echo "$R")
{ echo "$MSG"; } >> "$LOG"
if [[ $FAIL -eq 0 ]]; then
  notify "🧱 Rebuild drill ✅ homelab je plně obnovitelný z gitu + R2:"$'\n'"$MSG" || true; exit 0
else
  notify "🧱 Rebuild drill ❌ PROBLÉM s obnovitelností:"$'\n'"$MSG" || true; exit 1
fi
