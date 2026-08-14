#!/usr/bin/env bash
# ============================================================================
# homelab-backup.sh — denní záloha (spouští systemd timer, viz backup.timer)
#
# Zálohuje ŠIFROVANĚ (openssl AES-256, klíč mimo git) na Cloudflare R2:
#   1) pg globals (role+hesla) pro každý Postgres kontejner
#   2) každou explicitně povolenou PROD databázi zvlášť.
#      Rehearsal/system DB se VYNECHÁVAJÍ.
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
  "natetrader-supabase-db|supabase_admin|postgres"
  "loot-supabase-db|supabase_admin|postgres"
  "contentgen-postgres|contentgen|contentgen"
  "lifeadmin-supabase-db|supabase_admin|postgres"
  "hummy-supabase-db|supabase_admin|postgres"
  "explainact-supabase-db|supabase_admin|postgres"
  "dokploy-postgres|dokploy|dokploy"
)
BACKUP_KEY="/srv/homelab/secrets/freio-backup-key.txt"   # AES-256 pass (mimo git; kopie off-box!)
R2_REMOTE="r2:homelab-backups"
R2_DR_REMOTE="r2dr:homelab-backups-dr"
R2_CONF="/srv/homelab/secrets/rclone.conf"
KEEP_R2_DAYS=30
KEEP_R2_DR_DAYS=90
KUMA_PUSH_FILE="/srv/homelab/secrets/kuma-backup-push-url.txt"  # obsahuje jen URL (volitelné)
POSTIZ_ARTIFACT_BACKUP="/usr/local/sbin/postiz-artifact-backup.sh"
POSTIZ_MANIFEST_HELPER="/usr/local/libexec/postiz-backup-manifest.py"
POSTIZ_QUIESCED_CAPTURE="/usr/local/sbin/postiz-quiesced-capture.sh"
POSTIZ_POLICY_ATTESTER="/usr/local/sbin/postiz-r2-policy-attest.sh"
POSTIZ_WORKSPACE_CLEANUP="/usr/local/sbin/postiz-backup-workspace-cleanup.sh"
POSTIZ_STORAGE_POLICY="/var/lib/homelab-backup/postiz-storage-policy.json"
POSTIZ_PRIMARY="r2postiz:homelab-backups/postiz"
POSTIZ_DR="r2drpostiz:homelab-backups-dr/postiz"
STATE_ROOT=/var/lib/homelab-backup
RUN_ROOT=/run/homelab-backup
NIGHTLY_WORKSPACE_LOCK=$RUN_ROOT/nightly-workspace.lock

TS=$(date -u +%Y%m%dT%H%M%SZ)
NIGHTLY_PREFIX="nightly/${TS:0:4}-${TS:4:2}"
POSTIZ_SET_PREFIX="recovery-sets/${TS:0:4}-${TS:4:2}/$TS"
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || { echo "!! unsafe backup StateDirectory"; exit 1; }
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || { echo "!! unsafe backup RuntimeDirectory"; exit 1; }
[[ -x "$POSTIZ_WORKSPACE_CLEANUP" && -f "$POSTIZ_WORKSPACE_CLEANUP" && \
   ! -L "$POSTIZ_WORKSPACE_CLEANUP" && \
   "$(stat -Lc '%u:%g:%a:%h' "$POSTIZ_WORKSPACE_CLEANUP")" == 0:0:755:1 ]] \
  || { echo "!! unsafe backup workspace cleanup helper"; exit 1; }
[[ -f "$NIGHTLY_WORKSPACE_LOCK" && ! -L "$NIGHTLY_WORKSPACE_LOCK" && \
   "$(stat -Lc '%u:%g:%a:%h' "$NIGHTLY_WORKSPACE_LOCK")" == 0:0:600:1 ]] \
  || { echo "!! unsafe nightly workspace lock"; exit 1; }
exec 7<>"$NIGHTLY_WORKSPACE_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$NIGHTLY_WORKSPACE_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/7")" ]] \
  || { echo "!! nightly workspace lock descriptor/path drift"; exit 1; }
flock -n 7 || { echo "!! another nightly backup is running"; exit 1; }
"$POSTIZ_WORKSPACE_CLEANUP" --scope nightly --lock-held-fd 7 \
  || { echo "!! stale nightly workspace cleanup failed"; exit 1; }
WORK=$(mktemp -d "$STATE_ROOT/nightly.XXXXXX") \
  || { echo "!! cannot create nightly workspace"; exit 1; }
trap 'rm -rf --one-file-system -- "$WORK"' EXIT
FAIL=0
POSTIZ_FAIL=0
# R2 501 NotImplemented se objevuje na 1. pokusu a projde na retry (známá R2 flakiness):
# vyšší retries + single-part upload cutoff => spolehlivé doručení bez šumu.
RC="env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent rclone --config $R2_CONF --retries 5 --low-level-retries 10 --s3-upload-cutoff 5G"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
safe_root_file(){
  local path=$1 mode=$2
  [[ -f "$path" && ! -L "$path" && \
     "$(stat -Lc '%u:%g:%a:%h' "$path")" == "0:0:${mode}:1" ]]
}
enc(){ # enc <in> <out.enc>  — AES-256, salted, pbkdf2
  openssl enc -aes-256-cbc -pbkdf2 -salt -in "$1" -out "$2" -pass file:"$BACKUP_KEY"
}
resolve(){ docker ps --format '{{.Names}}' | grep -E "^$1" | head -1; }
required_target(){ return 1; }

log "── Homelab šifrovaná záloha $TS ──"
safe_root_file "$BACKUP_KEY" 600 && [[ -s "$BACKUP_KEY" ]] \
  || { echo "!! chybí nebo není root-only backup key $BACKUP_KEY"; exit 1; }
safe_root_file "$R2_CONF" 600 \
  || { echo "!! rclone config není root-only"; exit 1; }

# ── 1+2) Postgres: globals + per-DB dumpy, hned šifrované ─────────────────────
for target in "${BACKUP_TARGETS[@]}"; do
  IFS='|' read -r prefix user dbs <<< "$target"
  cname=$(resolve "$prefix")
  if [[ -z "$cname" ]]; then
    if required_target "$prefix"; then
      echo "!! povinný kontejner '$prefix' neběží"
      FAIL=1
    else
      echo "-- kontejner '$prefix' neběží — přeskakuji (pauznutá appka, nezálohuje se)"
    fi
    continue
  fi

  # globals (role+hesla) pro tento cluster
  if docker exec "$cname" pg_dumpall -U "$user" --globals-only > "$WORK/globals_${prefix}_$TS.sql" 2>/dev/null; then
    if enc "$WORK/globals_${prefix}_$TS.sql" "$WORK/globals_${prefix}_$TS.sql.enc"; then
      rm -f "$WORK/globals_${prefix}_$TS.sql"
    else
      echo "!! šifrování globals selhalo ($prefix)"; FAIL=1
      rm -f "$WORK/globals_${prefix}_$TS.sql" "$WORK/globals_${prefix}_$TS.sql.enc"
    fi
  else
    echo "!! globals selhaly ($prefix)"; FAIL=1
  fi

  for db in $dbs; do
    log "   pg_dump $prefix/$db"
    if docker exec "$cname" pg_dump -U "$user" -Fc -Z6 "$db" > "$WORK/db_${prefix}_${db}_$TS.dump" 2>/dev/null && [[ -s "$WORK/db_${prefix}_${db}_$TS.dump" ]]; then
      if enc "$WORK/db_${prefix}_${db}_$TS.dump" "$WORK/db_${prefix}_${db}_$TS.dump.enc"; then
        rm -f "$WORK/db_${prefix}_${db}_$TS.dump"
      else
        echo "!! šifrování dumpu selhalo ($prefix/$db)"; FAIL=1
        rm -f "$WORK/db_${prefix}_${db}_$TS.dump" "$WORK/db_${prefix}_${db}_$TS.dump.enc"
      fi
    else
      echo "!! selhal dump $prefix/$db"; FAIL=1; rm -f "$WORK/db_${prefix}_${db}_$TS.dump"
    fi
  done
done

# ── 3) Config bundle (reprodukovatelnost serveru) — šifrovaně ────────────────
# POZOR: --exclude MUSÍ být PŘED cestami (GNU tar je pozicní), jinak se ignoruje.
CFG_TAR="$WORK/config_$TS.tar.gz"
if tar --exclude='*/data' --exclude='*.log' --exclude='*/node_modules' \
       --exclude='*/__pycache__' --exclude='*/.git' --exclude='*/repo/node_modules' \
       -czf "$CFG_TAR" \
       -C / etc/dokploy \
       -C / srv/homelab/compose srv/homelab/self-healing srv/homelab/email-bot \
       -C / usr/local/bin/homelab-backup.sh \
       -C / usr/local/sbin/freio-analytics-retention \
       -C / etc/systemd/system/backup.service etc/systemd/system/backup.timer \
            etc/systemd/system/freio-analytics-retention.service \
            etc/systemd/system/freio-analytics-retention.timer \
            etc/systemd/system/self-healing.service etc/systemd/system/email-bot.service \
            etc/systemd/system/freio-email-outbox.service \
            etc/systemd/system/freio-email-outbox.timer \
       2>/dev/null; then
  if [[ -s "$CFG_TAR" ]]; then
    if enc "$CFG_TAR" "$CFG_TAR.enc"; then
      rm -f "$CFG_TAR"
    else
      echo "!! šifrování config bundle selhalo"; FAIL=1
      rm -f "$CFG_TAR" "$CFG_TAR.enc"
    fi
  else
    echo "!! config bundle prázdný"; FAIL=1
  fi
else
  echo "!! config tar selhal"; FAIL=1
  rm -f "$CFG_TAR" "$CFG_TAR.enc"
fi

# ── 3b) Postiz complete recovery point — one bounded writer fence ───────────
# The capture helper stops Postiz/Temporal/Redis under a durable journal, takes
# one WAL-consistent physical PostgreSQL cluster plus portable logical dumps,
# copies config/uploads/operator state, and restarts the exact containers before
# any R2 traffic. Plaintext remains only in the root-only StateDirectory.
POSTIZ_CAPTURE="$WORK/postiz-capture"
POSTIZ_PAYLOADS="$WORK/postiz-recovery-payloads"
POSTIZ_COMMIT="$WORK/postiz-recovery-commit"
mkdir -m 700 "$POSTIZ_PAYLOADS" "$POSTIZ_COMMIT"
if [[ ! -x "$POSTIZ_QUIESCED_CAPTURE" || ! -x "$POSTIZ_MANIFEST_HELPER" || \
      ! -x "$POSTIZ_ARTIFACT_BACKUP" || ! -x "$POSTIZ_POLICY_ATTESTER" ]] || \
   ! timeout --signal=TERM --kill-after=30s 1200s \
      "$POSTIZ_QUIESCED_CAPTURE" --timestamp "$TS" --output-dir "$POSTIZ_CAPTURE"; then
  echo "!! writer-fenced Postiz capture selhal"
  FAIL=1
  POSTIZ_FAIL=1
fi

encrypt_capture() {
  local source=$1 destination=$2
  [[ -s "$source" && ! -L "$source" ]] || return 1
  enc "$source" "$POSTIZ_PAYLOADS/$destination" || return 1
  rm -f -- "$source"
}

POSTIZ_GLOBALS_ENC="$POSTIZ_PAYLOADS/globals_postiz-postgres_$TS.sql.enc"
POSTIZ_DB_ENC="$POSTIZ_PAYLOADS/db_postiz-postgres_postiz_$TS.dump.enc"
POSTIZ_TEMPORAL_DB_ENC="$POSTIZ_PAYLOADS/db_postiz-postgres_temporal_$TS.dump.enc"
POSTIZ_TEMPORAL_VISIBILITY_DB_ENC="$POSTIZ_PAYLOADS/db_postiz-postgres_temporal_visibility_$TS.dump.enc"
POSTIZ_INSIGHTS_DB_ENC="$POSTIZ_PAYLOADS/db_postiz-postgres_insights_$TS.dump.enc"
POSTIZ_CLUSTER_ENC="$POSTIZ_PAYLOADS/postiz_postgres_cluster_$TS.tar.gz.enc"
POSTIZ_CAPTURE_EVIDENCE_ENC="$POSTIZ_PAYLOADS/postiz_capture_$TS.evidence.json.enc"
POSTIZ_CFG_ENC="$POSTIZ_PAYLOADS/postiz_config_$TS.tar.gz.enc"
POSTIZ_CONFIG_VOLUME_ENC="$POSTIZ_PAYLOADS/postiz_config_volume_$TS.tar.gz.enc"
POSTIZ_REDIS_ENC="$POSTIZ_PAYLOADS/postiz_redis_$TS.rdb.enc"
POSTIZ_CAPTURE_EVIDENCE_COPY="$POSTIZ_COMMIT/capture.evidence.json"
POSTIZ_RUNTIME_CONFIG_COPY="$POSTIZ_COMMIT/runtime-config.tar.gz"

if [[ "$POSTIZ_FAIL" -eq 0 ]]; then
  cp --reflink=auto --preserve=mode,ownership,timestamps -- \
    "$POSTIZ_CAPTURE/capture.evidence.json" "$POSTIZ_CAPTURE_EVIDENCE_COPY" && \
  cp --reflink=auto --preserve=mode,ownership,timestamps -- \
    "$POSTIZ_CAPTURE/runtime-config.tar.gz" "$POSTIZ_RUNTIME_CONFIG_COPY" && \
  cmp -s "$POSTIZ_CAPTURE/runtime-config.tar.gz" "$POSTIZ_RUNTIME_CONFIG_COPY" || {
      echo "!! kopie Postiz capture identity/runtime evidence selhala"
      FAIL=1
      POSTIZ_FAIL=1
    }
fi

if [[ "$POSTIZ_FAIL" -eq 0 ]]; then
  for spec in \
    "globals.sql|$(basename "$POSTIZ_GLOBALS_ENC")" \
    "database-postiz.dump|$(basename "$POSTIZ_DB_ENC")" \
    "database-temporal.dump|$(basename "$POSTIZ_TEMPORAL_DB_ENC")" \
    "database-temporal_visibility.dump|$(basename "$POSTIZ_TEMPORAL_VISIBILITY_DB_ENC")" \
    "database-insights.dump|$(basename "$POSTIZ_INSIGHTS_DB_ENC")" \
    "postgres-cluster.tar.gz|$(basename "$POSTIZ_CLUSTER_ENC")" \
    "capture.evidence.json|$(basename "$POSTIZ_CAPTURE_EVIDENCE_ENC")" \
    "runtime-config.tar.gz|$(basename "$POSTIZ_CFG_ENC")" \
    "config-volume.tar.gz|$(basename "$POSTIZ_CONFIG_VOLUME_ENC")" \
    "redis.rdb|$(basename "$POSTIZ_REDIS_ENC")"; do
    IFS='|' read -r source destination <<< "$spec"
    if ! encrypt_capture "$POSTIZ_CAPTURE/$source" "$destination"; then
      echo "!! šifrování Postiz capture payload selhalo: $source"
      FAIL=1
      POSTIZ_FAIL=1
    fi
  done
fi

SEASONAL_RELEASES_STATUS=absent
SEASONAL_REPLACEMENT_STATUS=absent
SEASONAL_POLICY_STATUS=absent
SEASONAL_RELEASES_ENC=
SEASONAL_REPLACEMENT_ENC=
SEASONAL_POLICY_ENC=
if [[ "$POSTIZ_FAIL" -eq 0 ]]; then
  SEASONAL_RELEASES_STATUS=$(tr -d '[:space:]' < "$POSTIZ_CAPTURE/seasonal-releases.status")
  SEASONAL_REPLACEMENT_STATUS=$(tr -d '[:space:]' < "$POSTIZ_CAPTURE/seasonal-anchor-replacement.status")
  SEASONAL_POLICY_STATUS=$(tr -d '[:space:]' < "$POSTIZ_CAPTURE/seasonal-policy.status")
  if [[ "$SEASONAL_RELEASES_STATUS" == present ]]; then
    SEASONAL_RELEASES_ENC="$POSTIZ_PAYLOADS/freio_content_seasonal_releases_$TS.tar.gz.enc"
    SEASONAL_REPLACEMENT_ENC="$POSTIZ_PAYLOADS/freio_content_seasonal_anchor_replacement_$TS.tar.gz.enc"
    SEASONAL_POLICY_ENC="$POSTIZ_PAYLOADS/freio_content_seasonal_backup_policy_$TS.json.enc"
    encrypt_capture "$POSTIZ_CAPTURE/seasonal-releases.tar.gz" "$(basename "$SEASONAL_RELEASES_ENC")" && \
      encrypt_capture "$POSTIZ_CAPTURE/seasonal-anchor-replacement.tar.gz" "$(basename "$SEASONAL_REPLACEMENT_ENC")" && \
      encrypt_capture "$POSTIZ_CAPTURE/seasonal-policy.json" "$(basename "$SEASONAL_POLICY_ENC")" || {
        echo "!! šifrování required seasonal rollback state selhalo"
        FAIL=1
        POSTIZ_FAIL=1
      }
  elif [[ "$SEASONAL_RELEASES_STATUS" != absent || "$SEASONAL_REPLACEMENT_STATUS" != absent || \
          "$SEASONAL_POLICY_STATUS" != absent ]]; then
    echo "!! seasonal capture status contract drifted"
    FAIL=1
    POSTIZ_FAIL=1
  fi
fi

POSTIZ_OPERATOR_STATE="$POSTIZ_COMMIT/postiz_operator_state_$TS.json"
POSTIZ_OPERATOR_STATE_ENC="$POSTIZ_PAYLOADS/postiz_operator_state_$TS.json.enc"
if [[ "$POSTIZ_FAIL" -eq 0 ]]; then
  operator_args=(
    --timestamp "$TS"
    --seasonal-releases-status "$SEASONAL_RELEASES_STATUS"
    --seasonal-anchor-replacement-status "$SEASONAL_REPLACEMENT_STATUS"
    --policy-status "$SEASONAL_POLICY_STATUS"
    --output "$POSTIZ_OPERATOR_STATE"
  )
  [[ "$SEASONAL_RELEASES_STATUS" == present ]] && operator_args+=(
    --seasonal-releases-archive "$SEASONAL_RELEASES_ENC"
    --seasonal-anchor-replacement-archive "$SEASONAL_REPLACEMENT_ENC"
    --policy-archive "$SEASONAL_POLICY_ENC"
  )
  if "$POSTIZ_MANIFEST_HELPER" write-operator-state "${operator_args[@]}" && \
      enc "$POSTIZ_OPERATOR_STATE" "$POSTIZ_OPERATOR_STATE_ENC"; then
    rm -f "$POSTIZ_OPERATOR_STATE"
  else
    echo "!! seasonal operator-state receipt selhal"
    FAIL=1
    POSTIZ_FAIL=1
  fi
fi

# Content-addressed blobs and all four running Docker images are published only
# after the exact writers have passed restart/readiness. `--immutable` in the
# child is a client guard; R2 Bucket Locks are the server-side retention control.
POSTIZ_ARTIFACT_RECEIPT="$POSTIZ_COMMIT/postiz_artifacts_$TS.json"
POSTIZ_ARTIFACT_ENC="$POSTIZ_PAYLOADS/postiz_artifacts_$TS.json.enc"
if [[ "$POSTIZ_FAIL" -eq 0 ]] && \
   "$POSTIZ_ARTIFACT_BACKUP" --timestamp "$TS" \
     --sealed-upload-root "$POSTIZ_CAPTURE/uploads-snapshot" \
     --sealed-upload-manifest "$POSTIZ_CAPTURE/uploads.json" \
     --capture-evidence "$POSTIZ_CAPTURE_EVIDENCE_COPY" \
     --runtime-config-archive "$POSTIZ_RUNTIME_CONFIG_COPY" \
     --expected-compose-sha256 "$(tr -d '[:space:]' < "$POSTIZ_CAPTURE/runtime-config.compose.sha256")" \
     --expected-dockerfile-sha256 "$(tr -d '[:space:]' < "$POSTIZ_CAPTURE/runtime-config.dockerfile.sha256")" \
     --receipt-out "$POSTIZ_ARTIFACT_RECEIPT" && \
   [[ -s "$POSTIZ_ARTIFACT_RECEIPT" ]] && \
   enc "$POSTIZ_ARTIFACT_RECEIPT" "$POSTIZ_ARTIFACT_ENC"; then
  rm -f "$POSTIZ_ARTIFACT_RECEIPT"
  rm -f "$POSTIZ_CAPTURE_EVIDENCE_COPY" "$POSTIZ_RUNTIME_CONFIG_COPY"
else
  echo "!! Postiz server-locked artifact backup selhal"
  FAIL=1
  POSTIZ_FAIL=1
  rm -f "$POSTIZ_ARTIFACT_RECEIPT" "$POSTIZ_ARTIFACT_ENC" \
    "$POSTIZ_CAPTURE_EVIDENCE_COPY" "$POSTIZ_RUNTIME_CONFIG_COPY"
fi

# ── 4) Secrets bundle (nejcitlivější) — šifrovaně ────────────────────────────
SEC_TAR="$WORK/secrets_$TS.tar.gz"
if tar -czf "$SEC_TAR" -C /srv/homelab secrets 2>/dev/null && [[ -s "$SEC_TAR" ]]; then
  if enc "$SEC_TAR" "$SEC_TAR.enc"; then
    rm -f "$SEC_TAR"
  else
    echo "!! šifrování secrets bundle selhalo"; FAIL=1
    rm -f "$SEC_TAR" "$SEC_TAR.enc"
  fi
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
NIGHTLY_PRIMARY_OK=0
NIGHTLY_DR_OK=0
if $RC copy "$WORK" "$R2_REMOTE/$NIGHTLY_PREFIX/" --max-depth 1 --include '*.enc' --transfers 4 -q; then
  if $RC check "$WORK" "$R2_REMOTE/$NIGHTLY_PREFIX/" \
      --max-depth 1 --include '*.enc' --one-way --checksum -q; then
    NIGHTLY_PRIMARY_OK=1
    log "✔ primární upload ověřen ($ENC_COUNT souborů → $R2_REMOTE/$NIGHTLY_PREFIX/)"
  else
    echo "!! kontrola integrity primárního R2 selhala"
    FAIL=1
  fi
  # retence: maž JEN uvnitř nightly/ (ne migration/ripieno point-in-time!)
  if ! $RC delete "$R2_REMOTE/nightly" --min-age "${KEEP_R2_DAYS}d" -q; then
    echo "!! retence primárního R2 selhala"
    FAIL=1
  fi

  # Sekundární DR bucket používá samostatný rclone remote. Chyba DR kopie
  # je chyba celé zálohy: primární objekt zůstane bezpečně uložený, ale
  # systemd/Kuma musí stav označit jako DOWN a poslat OnFailure upozornění.
  if $RC copy "$WORK" "$R2_DR_REMOTE/$NIGHTLY_PREFIX/" --max-depth 1 --include '*.enc' --transfers 4 -q; then
    if $RC check "$WORK" "$R2_DR_REMOTE/$NIGHTLY_PREFIX/" \
        --max-depth 1 --include '*.enc' --one-way --checksum -q; then
      NIGHTLY_DR_OK=1
      log "✔ DR kopie ověřena ($ENC_COUNT souborů → $R2_DR_REMOTE/$NIGHTLY_PREFIX/)"
    else
      echo "!! DR kontrola integrity selhala"
      FAIL=1
    fi
  else
    echo "!! DR copy selhalo"
    FAIL=1
  fi

  if ! $RC delete "$R2_DR_REMOTE/nightly" --min-age "${KEEP_R2_DR_DAYS}d" -q; then
    echo "!! retence DR bucketu selhala"
    FAIL=1
  fi
else
  echo "!! upload do R2 selhal"; FAIL=1
fi

# The authenticated commit record is last. Payloads live in a dedicated prefix
# whose retention is enforced by R2 Bucket Locks/lifecycle. Cloudflare Object
# Read & Write credentials are delete-capable, but this code never issues delete.
POSTIZ_RECOVERY_JSON="$POSTIZ_COMMIT/recovery-set.json"
POSTIZ_RECOVERY_ENC="$POSTIZ_COMMIT/recovery-set.json.enc"
POSTIZ_COMMIT_AUTH="$POSTIZ_COMMIT/COMMITTED.hmac.json"
POSTIZ_STORAGE_POLICY_ENC="$POSTIZ_PAYLOADS/postiz_storage_policy_$TS.json.enc"
if [[ "$POSTIZ_FAIL" -eq 0 ]] && \
   "$POSTIZ_POLICY_ATTESTER" && \
   [[ -f "$POSTIZ_STORAGE_POLICY" && ! -L "$POSTIZ_STORAGE_POLICY" && \
      "$(stat -Lc '%u:%g:%a:%h' "$POSTIZ_STORAGE_POLICY")" == 0:0:600:1 ]] && \
   "$POSTIZ_MANIFEST_HELPER" verify-storage-policy --policy "$POSTIZ_STORAGE_POLICY" && \
   enc "$POSTIZ_STORAGE_POLICY" "$POSTIZ_STORAGE_POLICY_ENC"; then
  :
else
  echo "!! čerstvé read-only ověření R2 Bucket Lock/lifecycle selhalo"
  FAIL=1
  POSTIZ_FAIL=1
  rm -f "$POSTIZ_STORAGE_POLICY_ENC"
fi
required_postiz_payloads=(
  "$POSTIZ_GLOBALS_ENC" "$POSTIZ_DB_ENC" "$POSTIZ_TEMPORAL_DB_ENC"
  "$POSTIZ_TEMPORAL_VISIBILITY_DB_ENC" "$POSTIZ_INSIGHTS_DB_ENC"
  "$POSTIZ_CLUSTER_ENC" "$POSTIZ_CAPTURE_EVIDENCE_ENC" "$POSTIZ_CFG_ENC"
  "$POSTIZ_CONFIG_VOLUME_ENC" "$POSTIZ_REDIS_ENC" "$POSTIZ_ARTIFACT_ENC"
  "$POSTIZ_OPERATOR_STATE_ENC" "$POSTIZ_STORAGE_POLICY_ENC"
)
for payload in "${required_postiz_payloads[@]}"; do
  [[ -s "$payload" && ! -L "$payload" ]] || POSTIZ_FAIL=1
done

if [[ "$POSTIZ_FAIL" -eq 0 ]] && \
   "$POSTIZ_MANIFEST_HELPER" write-recovery-set \
     --timestamp "$TS" \
     --physical-cluster "$POSTIZ_CLUSTER_ENC" \
     --capture-evidence "$POSTIZ_CAPTURE_EVIDENCE_ENC" \
     --globals "$POSTIZ_GLOBALS_ENC" \
     --database-postiz "$POSTIZ_DB_ENC" \
     --database-temporal "$POSTIZ_TEMPORAL_DB_ENC" \
     --database-temporal-visibility "$POSTIZ_TEMPORAL_VISIBILITY_DB_ENC" \
     --database-insights "$POSTIZ_INSIGHTS_DB_ENC" \
     --runtime-config "$POSTIZ_CFG_ENC" \
     --config-volume "$POSTIZ_CONFIG_VOLUME_ENC" \
     --redis "$POSTIZ_REDIS_ENC" \
     --artifacts "$POSTIZ_ARTIFACT_ENC" \
     --operator-state "$POSTIZ_OPERATOR_STATE_ENC" \
     --storage-policy "$POSTIZ_STORAGE_POLICY_ENC" \
     --output "$POSTIZ_RECOVERY_JSON" && \
   enc "$POSTIZ_RECOVERY_JSON" "$POSTIZ_RECOVERY_ENC" && \
   "$POSTIZ_MANIFEST_HELPER" write-auth-record \
     --cipher "$POSTIZ_RECOVERY_ENC" \
     --key-file "$BACKUP_KEY" \
     --context "postiz-recovery-set:$TS" \
     --output "$POSTIZ_COMMIT_AUTH" && \
   "$POSTIZ_MANIFEST_HELPER" verify-auth-record \
     --cipher "$POSTIZ_RECOVERY_ENC" \
     --record "$POSTIZ_COMMIT_AUTH" \
     --key-file "$BACKUP_KEY" \
     --expected-context "postiz-recovery-set:$TS"; then
  rm -f "$POSTIZ_RECOVERY_JSON"
  postiz_remote_ok=1
  for remote in "$POSTIZ_PRIMARY" "$POSTIZ_DR"; do
    if ! $RC copy "$POSTIZ_PAYLOADS" "$remote/$POSTIZ_SET_PREFIX" \
        --include '*.enc' --immutable --checksum --transfers 4 -q || \
       ! $RC check "$POSTIZ_PAYLOADS" "$remote/$POSTIZ_SET_PREFIX" \
        --include '*.enc' --one-way --checksum -q; then
      echo "!! Postiz recovery payload upload/check selhal: $remote"
      postiz_remote_ok=0
    fi
  done
  if [[ "$postiz_remote_ok" -eq 1 ]] && \
     $RC copyto "$POSTIZ_RECOVERY_ENC" \
       "$POSTIZ_PRIMARY/$POSTIZ_SET_PREFIX/recovery-set.json.enc" --immutable --checksum -q && \
     $RC copyto "$POSTIZ_RECOVERY_ENC" \
       "$POSTIZ_DR/$POSTIZ_SET_PREFIX/recovery-set.json.enc" --immutable --checksum -q && \
     $RC check "$POSTIZ_COMMIT" "$POSTIZ_PRIMARY/$POSTIZ_SET_PREFIX" \
       --include 'recovery-set.json.enc' --one-way --checksum -q && \
     $RC check "$POSTIZ_COMMIT" "$POSTIZ_DR/$POSTIZ_SET_PREFIX" \
       --include 'recovery-set.json.enc' --one-way --checksum -q && \
     $RC copyto "$POSTIZ_COMMIT_AUTH" \
       "$POSTIZ_PRIMARY/$POSTIZ_SET_PREFIX/COMMITTED.hmac.json" --immutable --checksum -q && \
     $RC copyto "$POSTIZ_COMMIT_AUTH" \
       "$POSTIZ_DR/$POSTIZ_SET_PREFIX/COMMITTED.hmac.json" --immutable --checksum -q && \
     $RC check "$POSTIZ_COMMIT" "$POSTIZ_PRIMARY/$POSTIZ_SET_PREFIX" \
       --include 'COMMITTED.hmac.json' --one-way --checksum -q && \
     $RC check "$POSTIZ_COMMIT" "$POSTIZ_DR/$POSTIZ_SET_PREFIX" \
       --include 'COMMITTED.hmac.json' --one-way --checksum -q; then
    log "✔ authenticated Postiz recovery set committed: $TS"
  else
    echo "!! authenticated Postiz recovery-set commit selhal"
    FAIL=1
    POSTIZ_FAIL=1
  fi
else
  echo "!! Postiz recovery payload/policy/auth contract není kompletní; commit nevznikl"
  FAIL=1
  POSTIZ_FAIL=1
fi

# ── 6) Report + Kuma push ────────────────────────────────────────────────────
kuma_push() {
  local status=$1 message=$2 url extra url_fd path_metadata fd_metadata
  [[ ! -e "$KUMA_PUSH_FILE" && ! -L "$KUMA_PUSH_FILE" ]] && return 0
  exec {url_fd}<"$KUMA_PUSH_FILE" || return 1
  path_metadata=$(stat -Lc '%u:%g:%a:%h:%d:%i' "$KUMA_PUSH_FILE") || return 1
  fd_metadata=$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/$url_fd") || return 1
  [[ ! -L "$KUMA_PUSH_FILE" && "$path_metadata" == "0:0:600:1:"* && \
     "$path_metadata" == "$fd_metadata" ]] || return 1
  IFS= read -r url <&"$url_fd" || [[ -n "$url" ]] || return 1
  extra=
  if IFS= read -r extra <&"$url_fd" || [[ -n "$extra" ]]; then
    return 1
  fi
  exec {url_fd}<&-
  [[ "$url" =~ ^https://[-A-Za-z0-9._~:/?@!$%\&*+,\;=]+$ ]] || return 1
  printf 'url = "%s?status=%s&msg=%s&ping="\nsilent\nshow-error\nfail\nproto = "=https"\nnoproxy = "*"\nmax-time = 10\n' \
    "$url" "$status" "$message" | \
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
      curl --disable --config - >/dev/null 2>&1
}
if [[ $FAIL -eq 0 ]]; then
  log "✔ Záloha OK ($ENC_COUNT šifrovaných souborů)"
  kuma_push up OK || true
  exit 0
else
  echo "✘ ZÁLOHA SELHALA — viz log výše"
  kuma_push down FAIL || true
  exit 1
fi
