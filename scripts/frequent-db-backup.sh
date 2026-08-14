#!/usr/bin/env bash
# ============================================================================
# frequent-db-backup.sh — časté ŠIFROVANÉ per-DB PIT dumpy á 10 min → primary R2.
# Lehčí než nightly (jen DB, žádný config/secrets bundle). Krátká retence 48h.
# Doplněk nočních záloh (ty drží 30 dní + config + secrets).
# ============================================================================
set -uo pipefail
BACKUP_KEY="/srv/homelab/secrets/freio-backup-key.txt"
R2_REMOTE="r2:homelab-backups"
R2_CONF="/srv/homelab/secrets/rclone.conf"
KEEP_HOURS=48
TS=$(date -u +%Y%m%dT%H%M%SZ)
PREFIX="frequent/${TS:0:4}-${TS:4:2}-${TS:6:2}"
STATE_ROOT=/var/lib/homelab-backup
RUN_ROOT=/run/homelab-backup
MUTATION_LOCK=$RUN_ROOT/postiz-mutation.lock
WORKSPACE_LOCK=$RUN_ROOT/frequent-workspace.lock
WORKSPACE_CLEANUP=/usr/local/sbin/postiz-backup-workspace-cleanup.sh
[[ -d "$STATE_ROOT" && ! -L "$STATE_ROOT" && "$(stat -Lc '%u:%g:%a' "$STATE_ROOT")" == 0:0:700 ]] \
  || { echo "unsafe backup StateDirectory"; exit 1; }
[[ -d "$RUN_ROOT" && ! -L "$RUN_ROOT" && "$(stat -Lc '%u:%g:%a' "$RUN_ROOT")" == 0:0:700 ]] \
  || { echo "unsafe backup RuntimeDirectory"; exit 1; }
[[ -f "$MUTATION_LOCK" && ! -L "$MUTATION_LOCK" && \
   "$(stat -Lc '%u:%g:%a:%h' "$MUTATION_LOCK")" == 0:0:600:1 ]] \
  || { echo "unsafe shared Postiz mutation lock"; exit 1; }
[[ -f "$WORKSPACE_LOCK" && ! -L "$WORKSPACE_LOCK" && \
   "$(stat -Lc '%u:%g:%a:%h' "$WORKSPACE_LOCK")" == 0:0:600:1 ]] \
  || { echo "unsafe frequent workspace lock"; exit 1; }
[[ -x "$WORKSPACE_CLEANUP" && -f "$WORKSPACE_CLEANUP" && ! -L "$WORKSPACE_CLEANUP" && \
   "$(stat -Lc '%u:%g:%a:%h' "$WORKSPACE_CLEANUP")" == 0:0:755:1 ]] \
  || { echo "unsafe backup workspace cleanup helper"; exit 1; }
exec 7<>"$WORKSPACE_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$WORKSPACE_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/7")" ]] \
  || { echo "frequent workspace lock descriptor/path drift"; exit 1; }
flock -n 7 || { echo "another frequent backup is running"; exit 1; }
"$WORKSPACE_CLEANUP" --scope frequent --lock-held-fd 7 \
  || { echo "stale frequent workspace cleanup failed"; exit 1; }
WORK=$(mktemp -d "$STATE_ROOT/frequent.XXXXXX") \
  || { echo "cannot create frequent workspace"; exit 1; }
trap 'rm -rf --one-file-system -- "$WORK"' EXIT
# Whole-operation retries absorb transient R2/S3 edge failures; low-level
# retries cover individual HTTP transport failures.
RC="env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/nonexistent rclone --config $R2_CONF --retries 8 --retries-sleep 20s --low-level-retries 10 --s3-upload-cutoff 5G"
# Stav poslední ÚSPĚŠNÉ zálohy — staleness-gate proti flappy alertům při krátkém výpadku R2.
STATE="$STATE_ROOT/frequent-backup-last-ok"
STALE_SEC=2700   # 45 min alert window for missing complete per-DB PIT coverage; not service RPO.
FAIL=0
exec 8<>"$MUTATION_LOCK"
[[ "$(stat -Lc '%u:%g:%a:%h:%d:%i' "$MUTATION_LOCK")" == \
   "$(stat -Lc '%u:%g:%a:%h:%d:%i' "/proc/$$/fd/8")" ]] \
  || { echo "shared lock descriptor/path drift"; exit 1; }

# container_prefix|user|"db1 db2"
TARGETS=(
  "shared-postgres|postgres|lokwave inngest"
  "ripieno-postgres|postgres|ripieno"
  "launchmail-postgres|postgres|launchmail"
  "postiz-postgres|postiz|postiz temporal temporal_visibility insights"
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
required_target(){ [[ "$1" == postiz-postgres ]]; }
assert_postiz_identity() {
  [[ "$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}' \
      postiz-postgres 2>/dev/null)" == "$postiz_identity" ]]
}
[[ -s "$BACKUP_KEY" ]] || { echo "chybí klíč"; exit 1; }

for t in "${TARGETS[@]}"; do
  IFS='|' read -r pfx user dbs <<< "$t"
  postiz_lock_held=0
  if required_target "$pfx"; then
    if ! flock -w 120 8; then
      echo "!! Postiz mutation lock je obsazen"; FAIL=1; continue
    fi
    postiz_lock_held=1
    postiz_identity=$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}' \
      postiz-postgres 2>/dev/null || true)
    IFS='|' read -r c postiz_image running paused <<< "$postiz_identity"
    if [[ ! "$c" =~ ^[0-9a-f]{64}$ || ! "$postiz_image" =~ ^sha256:[0-9a-f]{64}$ || \
         "$running|$paused" != true\|false ]]; then
      c=
    else
      inventory=$(timeout 20s docker exec "$c" psql -X -v ON_ERROR_STOP=1 -U postiz \
        -d postgres -Atc 'SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1' \
        2>/dev/null || true)
      expected_inventory='insights
postgres
postiz
temporal
temporal_visibility'
      postgres_user_objects=$(timeout 20s docker exec "$c" psql -X -v ON_ERROR_STOP=1 \
        -U postiz -d postgres -Atc "
          SELECT
            (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%')
            +
            (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%')" 2>/dev/null || true)
      [[ "$inventory" == "$expected_inventory" && "$postgres_user_objects" == 0 ]] || c=
    fi
  else
    c=$(resolve "$pfx")
  fi
  if [[ -z "$c" ]]; then
    if required_target "$pfx"; then
      echo "!! povinný kontejner $pfx neběží"; FAIL=1
    else
      echo "-- $pfx neběží — přeskakuji (pauznutá appka, nezálohuje se)"
    fi
    ((postiz_lock_held)) && flock -u 8
    continue
  fi
  for db in $dbs; do
    if required_target "$pfx" && ! assert_postiz_identity; then
      echo "!! Postiz container identity drifted during PIT set"; FAIL=1; break
    fi
    if timeout --signal=TERM --kill-after=10s 90s docker exec \
        -e PGOPTIONS='-c statement_timeout=75000 -c lock_timeout=3000' \
        "$c" pg_dump -U "$user" -Fc -Z6 "$db" \
        > "$WORK/db_${pfx}_${db}_$TS.dump" 2>/dev/null && \
       [[ -s "$WORK/db_${pfx}_${db}_$TS.dump" ]] && \
       { ! required_target "$pfx" || assert_postiz_identity; }; then
      if openssl enc -aes-256-cbc -pbkdf2 -salt -in "$WORK/db_${pfx}_${db}_$TS.dump" \
          -out "$WORK/db_${pfx}_${db}_$TS.dump.enc" -pass file:"$BACKUP_KEY"; then
        rm -f "$WORK/db_${pfx}_${db}_$TS.dump"
      else
        echo "!! šifrování dumpu $pfx/$db selhalo"; FAIL=1
        rm -f "$WORK/db_${pfx}_${db}_$TS.dump" "$WORK/db_${pfx}_${db}_$TS.dump.enc"
      fi
    else echo "!! dump $pfx/$db selhal"; FAIL=1; rm -f "$WORK/db_${pfx}_${db}_$TS.dump"; fi
  done
  ((postiz_lock_held)) && flock -u 8
done

N=$(ls "$WORK"/*.enc 2>/dev/null | wc -l)
if [[ "$N" -gt 0 ]] && $RC copy "$WORK" "$R2_REMOTE/$PREFIX/" --include '*.enc' --transfers 4 -q && \
   $RC check "$WORK" "$R2_REMOTE/$PREFIX/" --include '*.enc' --one-way --checksum -q; then
  $RC delete "$R2_REMOTE/frequent" --min-age "${KEEP_HOURS}h" -q || true
  if [[ $FAIL -eq 0 ]]; then
    state_tmp=$(mktemp "$STATE_ROOT/.frequent-last-ok.XXXXXX")
    date +%s > "$state_tmp"
    chmod 600 "$state_tmp"
    sync -f "$state_tmp"
    mv -f "$state_tmp" "$STATE"
    sync -f "$STATE_ROOT"
    echo "✔ frequent per-DB PIT set OK ($N DB, $TS)"
    exit 0
  fi
  echo "✘ partial per-DB PIT upload; overall last-ok was not advanced"
  exit 1
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
