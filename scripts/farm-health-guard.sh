#!/usr/bin/env bash
# farm-health-guard.sh — hlídá, jestli se agent-farm nevrátila do chybové smyčky.
#
# Kontext: 10. 8. 2026 farma vyrobila 21 tis. pokusů za den a 3 úspěchy — točila se
# na otráveném git worktree, ze kterého se neuměla dostat. Opraveno, ale takový
# regres by jinak nikdo nezpozoroval (nic nespadne, jen se nic neudělá).
# Tenhle hlídač to pozná a pošle Telegram, než to sežere den práce.
#
# Signály:
#   1) churn      — >150 selhání/hod, nebo >60 selhání do 5 s (typický podpis)
#   2) fan-out    — fronta má víc než 3 zprávy na tentýž task (rozbitá dedup)
#   3) ticho      — 0 pokusů za 6 h, ačkoli existují připravené tasky (uvázlý dispatch)
set -uo pipefail

DBQ(){ docker exec -i agentfarm-supabase-db-1 psql -U supabase_admin -d postgres -tAc "$1" 2>/dev/null | tr -d ' \n'; }
alert(){
  local msg="$1"
  echo "[farm-health-guard] ALERT: $msg"
  local tok chat
  tok="$(grep -m1 TELEGRAM_BOT_TOKEN /srv/frem/.env 2>/dev/null | cut -d= -f2 | tr -d '\r\n')"
  chat="$(grep -m1 TELEGRAM_CHAT_ID /srv/frem/.env 2>/dev/null | cut -d= -f2 | tr -d '\r\n')"
  [ -n "$tok" ] && [ -n "$chat" ] && curl -s -X POST "https://api.telegram.org/bot$tok/sendMessage" \
    -d "chat_id=$chat" -d "text=🚜 agent-farm: $msg" >/dev/null || true
}

FAILED_1H="$(DBQ "select count(*) from attempts where started_at > now() - interval '1 hour' and status='failed';")"
INSTANT_1H="$(DBQ "select count(*) from attempts where started_at > now() - interval '1 hour' and status='failed' and wall_ms < 5000;")"
MAXDUP="$(DBQ "select coalesce(max(cnt),0) from (select count(*) cnt from pgmq.q_q_tasks group by message->>'taskId') s;")"
ATT_6H="$(DBQ "select count(*) from attempts where started_at > now() - interval '6 hours';")"
READY="$(DBQ "select count(*) from tasks t join projects p on p.id=t.project_id where t.status='queued' and p.status='active';")"

# Zastavená farma nemá co diagnostikovat. Bez tohohle hlídač každou hodinu hlásí
# "uvázlý dispatch: 0 pokusů, přitom čeká N tasků" a "fan-out fronty" — obojí je
# u schválně vypnuté farmy pravda a zároveň to není problém. Takový poplach jen
# otupí pozornost a překryje ten okamžik, kdy se pokazí něco doopravdy.
PAUSED="$(DBQ "select coalesce((select value::text from farm_settings where key='global_pause'),'false');")"
if [ "${PAUSED:-false}" = "true" ]; then
  SRC="$(DBQ "select coalesce((select value::text from farm_settings where key='pause_source'),'?');")"
  echo "[farm-health-guard] farma je zastavena (${SRC}) — kontroly přeskočeny"
  exit 0
fi

problems=()
[ "${FAILED_1H:-0}" -gt 150 ] && problems+=("churn: ${FAILED_1H} selhání/hod")
[ "${INSTANT_1H:-0}" -gt 60 ] && problems+=("okamžitá selhání: ${INSTANT_1H}/hod (podpis otráveného worktree)")
[ "${MAXDUP:-0}" -gt 3 ] && problems+=("fan-out fronty: ${MAXDUP} zpráv na jeden task (rozbitá dedup v reconciliation)")
[ "${ATT_6H:-0}" -eq 0 ] && [ "${READY:-0}" -gt 0 ] && problems+=("uvázlý dispatch: 0 pokusů za 6 h, přitom čeká ${READY} tasků")

# Zaseknutá PŘÍPRAVA pokusu: běží dlouho, ale neudělal ani krok. Tenhle stav
# 22. 8. položil farmu na 15 hodin — dva takové pokusy obsadily obě místa workerů.
# Hlídač to tehdy nahlásil jen jako "uvázlý dispatch", což neřeklo, kde hledat.
STUCK_SETUP="$(DBQ "select count(*) from attempts where status='running' and steps_used = 0 and started_at < now() - interval '45 minutes';")"
[ "${STUCK_SETUP:-0}" -gt 0 ] && problems+=("${STUCK_SETUP} pokusů visí v přípravě přes 45 min s 0 kroky (zabírají místa workerů — zabij jim kontejnery a ukonči pokusy)")

if [ ${#problems[@]} -gt 0 ]; then
  alert "$(printf '%s; ' "${problems[@]}")— zkontroluj orchestrátor"
  exit 1
fi
echo "[farm-health-guard] OK (selhání/hod=${FAILED_1H}, instant=${INSTANT_1H}, max dup=${MAXDUP}, pokusy/6h=${ATT_6H})"
