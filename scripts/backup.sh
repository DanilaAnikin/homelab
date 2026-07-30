#!/usr/bin/env bash
# ============================================================================
# homelab-backup.sh — denní záloha (spouští systemd timer, viz backup.timer)
#
# Zálohuje ŠIFROVANĚ (openssl AES-256, klíč mimo git) na Cloudflare R2:
#   1) pg globals (role+hesla) pro každý Postgres kontejner
#   2) každou PROD databázi zvlášť: freio, lokwave, inngest, ripieno,
#      launchmail, dokploy(metadata). Rehearsal/system DB se VYNECHÁVAJÍ.
#   3) config bundle: /etc/dokploy + /srv/homelab/{compose,self-healing,email-bot}
#      + relevantní systemd unity (reprodukovatelnost celého serveru)
#   4) secrets bundle: /srv/homelab/secrets (nejcitlivější, šifrované)
#
# VŠE se před odvozem na R2 šifruje (žádné plaintext PII off-site).
# Fail-loud: jakýkoli dílčí neúspěch => exit 1 => systemd OnFailure => Telegram.
# Úspěch hlásí do Uptime Kuma push monitoru (missing heartbeat => alert).
# ============================================================================
set -uo pipefail   # bez -e: chceme doběhnout všechno a pak reportovat

# ── Konfigurace ─────────────────────────────────────────────────────────────
# container_prefix|pg_user|"db1 db2 ..."   (explicitní DB seznam = žádné rehearsal/system DB)
BACKUP_TARGETS=(
  "shared-postgres|postgres|lokwave inngest"
  "ripieno-postgres|postgres|ripieno"
  "launchmail-postgres|postgres|launchmail"
  "supabase-db|postgres|freio"
  "gorillatype-supabase-db|supabase_admin|postgres"
  "classio-supabase-db|supabase_admin|postgres"
  "dokploy-postgres|dokploy|dokploy"
)
BACKUP_KEY="/srv/homelab/secrets/freio-backup-key.txt"   # AES-256 pass (mimo git; kopie off-box!)
R2_REMOTE="r2:homelab-backups"
R2_CONF="/srv/homelab/secrets/rclone.conf"
NIGHTLY_PREFIX="nightly/$(date +%Y-%m)"      # scoped prefix — retence maže JEN zde (ne freio-migration/ripieno)
KEEP_R2_DAYS=30
KUMA_PUSH_FILE="/srv/homelab/secrets/kuma-backup-push-url.txt"  # obsahuje jen URL (volitelné)

TS=$(date +%Y%m%dT%H%M%SZ)
WORK=$(mktemp -d /tmp/pgbackup.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
FAIL=0
# R2 501 NotImplemented se objevuje na 1. pokusu a projde na retry (známá R2 flakiness):
# vyšší retries + single-part upload cutoff => spolehlivé doručení bez šumu.
RC="rclone --config $R2_CONF --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
enc(){ # enc <in> <out.enc>  — AES-256, salted, pbkdf2
  openssl enc -aes-256-cbc -pbkdf2 -salt -in "$1" -out "$2" -pass file:"$BACKUP_KEY"
}
resolve(){ docker ps --format '{{.Names}}' | grep -E "^$1" | head -1; }

log "── Homelab šifrovaná záloha $TS ──"
[[ -s "$BACKUP_KEY" ]] || { echo "!! chybí backup key $BACKUP_KEY"; exit 1; }

# ── 1+2) Postgres: globals + per-DB dumpy, hned šifrované ─────────────────────
for target in "${BACKUP_TARGETS[@]}"; do
  IFS='|' read -r prefix user dbs <<< "$target"
  cname=$(resolve "$prefix")
  if [[ -z "$cname" ]]; then echo "!! kontejner '$prefix' neběží — přeskakuji"; FAIL=1; continue; fi

  # globals (role+hesla) pro tento cluster
  if docker exec "$cname" pg_dumpall -U "$user" --globals-only > "$WORK/globals_${prefix}_$TS.sql" 2>/dev/null; then
    enc "$WORK/globals_${prefix}_$TS.sql" "$WORK/globals_${prefix}_$TS.sql.enc" && rm -f "$WORK/globals_${prefix}_$TS.sql"
  else
    echo "!! globals selhaly ($prefix)"; FAIL=1
  fi

  for db in $dbs; do
    log "   pg_dump $prefix/$db"
    if docker exec "$cname" pg_dump -U "$user" -Fc -Z6 "$db" > "$WORK/db_${prefix}_${db}_$TS.dump" 2>/dev/null && [[ -s "$WORK/db_${prefix}_${db}_$TS.dump" ]]; then
      enc "$WORK/db_${prefix}_${db}_$TS.dump" "$WORK/db_${prefix}_${db}_$TS.dump.enc" && rm -f "$WORK/db_${prefix}_${db}_$TS.dump"
    else
      echo "!! selhal dump $prefix/$db"; FAIL=1; rm -f "$WORK/db_${prefix}_${db}_$TS.dump"
    fi
  done
done

# ── 3) Config bundle (reprodukovatelnost serveru) — šifrovaně ────────────────
# POZOR: --exclude MUSÍ být PŘED cestami (GNU tar je pozicní), jinak se ignoruje.
CFG_TAR="$WORK/config_$TS.tar.gz"
tar --exclude='*/data' --exclude='*.log' --exclude='*/node_modules' \
    --exclude='*/__pycache__' --exclude='*/.git' --exclude='*/repo/node_modules' \
    -czf "$CFG_TAR" \
    -C / etc/dokploy \
    -C / srv/homelab/compose srv/homelab/self-healing srv/homelab/email-bot \
    2>/dev/null || echo "!! config tar částečně selhal (pokračuji)"
# systemd unity homelabu
tar -rf "${CFG_TAR%.gz}" -C /usr/local/bin homelab-backup.sh 2>/dev/null || true
tar -rf "${CFG_TAR%.gz}" -C /etc/systemd/system \
  backup.service backup.timer self-healing.service email-bot.service \
  freio-email-outbox.service freio-email-outbox.timer 2>/dev/null || true
if [[ -s "$CFG_TAR" ]]; then
  enc "$CFG_TAR" "$CFG_TAR.enc" && rm -f "$CFG_TAR"
else
  echo "!! config bundle prázdný"; FAIL=1
fi

# ── 4) Secrets bundle (nejcitlivější) — šifrovaně ────────────────────────────
SEC_TAR="$WORK/secrets_$TS.tar.gz"
if tar -czf "$SEC_TAR" -C /srv/homelab secrets 2>/dev/null && [[ -s "$SEC_TAR" ]]; then
  enc "$SEC_TAR" "$SEC_TAR.enc" && rm -f "$SEC_TAR"
else
  echo "!! secrets bundle selhal"; FAIL=1
fi

# ── 4b) Frem: hlas a scénáře (jediné, co nejde vyrobit znovu zdarma) ─────────
# Snímky i hotové video se dají přegenerovat bez nákladů (Higgsfield Unlimited),
# ale hlas z ElevenLabs stojí reálné peníze (~0,60 $ na video, přes 27 videí
# už ~16 $). Záloha je proto úzká: hlas, časy a texty. Snímky (750 MB na video)
# a final.mp4 se nezálohují schválně.
#
# NEŠIFRUJE se, a to ze dvou důvodů: (1) hlas i texty jsou určené k publikaci
# na YouTube, takže tu není co chránit, (2) šifrování dá při každém běhu jiný
# ciphertext, takže by se každou noc znovu vozily celé gigabajty. Takhle
# rclone copy přeskočí, co už na R2 leží, a jede jen nový hlas (~33 MB/video).
#
# Retence maže jen uvnitř nightly/, takže tenhle prefix zůstává navždy.
FREM_VIDEOS=/srv/frem/repo/videos
if [[ -d "$FREM_VIDEOS" ]]; then
  log "   Frem: hlas + scénáře → R2"
  if $RC copy "$FREM_VIDEOS" "$R2_REMOTE/frem/videos/" \
      --include '*/voice.wav' --include '*/transcription.txt' \
      --include '*/timestamps.json' --include '*/script.md' \
      --include '*/package.md' --include '*/images/prompts.tsv' \
      --transfers 4 -q; then
    FREM_N=$($RC ls "$R2_REMOTE/frem/videos/" 2>/dev/null | grep -c 'voice.wav' || true)
    log "✔ Frem záloha OK (hlasů na R2: ${FREM_N:-?})"
  else
    echo "!! záloha Fremu selhala"; FAIL=1
  fi
fi

# ── 5) Odvoz na R2 (jen .enc soubory) ────────────────────────────────────────
ENC_COUNT=$(ls "$WORK"/*.enc 2>/dev/null | wc -l)
if [[ "$ENC_COUNT" -eq 0 ]]; then echo "!! žádné .enc k odvozu"; FAIL=1; fi
if $RC copy "$WORK" "$R2_REMOTE/$NIGHTLY_PREFIX/" --include '*.enc' --transfers 4 -q; then
  log "✔ upload OK ($ENC_COUNT souborů → $R2_REMOTE/$NIGHTLY_PREFIX/)"
  # retence: maž JEN uvnitř nightly/ (ne migration/ripieno point-in-time!)
  $RC delete "$R2_REMOTE/nightly" --min-age "${KEEP_R2_DAYS}d" -q || true
  # 2. off-site (DR bucket, delší 90d retence — chrání proti retenci/smazání primárního)
  $RC copy "$WORK" "r2:homelab-backups-dr/$NIGHTLY_PREFIX/" --include '*.enc' --transfers 4 -q || echo "!! DR copy selhalo (nefatální)"
  $RC delete "r2:homelab-backups-dr" --min-age 90d -q || true
else
  echo "!! upload do R2 selhal"; FAIL=1
fi

# ── 6) Report + Kuma push ────────────────────────────────────────────────────
KUMA_URL=""; [[ -s "$KUMA_PUSH_FILE" ]] && KUMA_URL=$(tr -d '[:space:]' < "$KUMA_PUSH_FILE")
if [[ $FAIL -eq 0 ]]; then
  log "✔ Záloha OK ($ENC_COUNT šifrovaných souborů)"
  [[ -n "$KUMA_URL" ]] && curl -fsS -m 10 "$KUMA_URL?status=up&msg=OK&ping=" >/dev/null 2>&1 || true
  exit 0
else
  echo "✘ ZÁLOHA SELHALA — viz log výše"
  [[ -n "$KUMA_URL" ]] && curl -fsS -m 10 "$KUMA_URL?status=down&msg=FAIL" >/dev/null 2>&1 || true
  exit 1
fi
