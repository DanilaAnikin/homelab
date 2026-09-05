#!/usr/bin/env bash
# Hlídač publikování v Postizu.
#
# Proč vznikl: 16. 8. osiřel uvnitř kontejneru `next-server` na portu 4200,
# pm2 kvůli EADDRINUSE restartoval frontend ~22×/min, vytížil kontejner na
# 120 % CPU a 87 % paměti a tím vyhladověl orchestrátor (Temporal worker).
# Publikování stálo 11 dní a NIC to nenahlásilo — kontejner byl „up", Kuma
# svítila zeleně, jen posty tiše zůstávaly ve frontě.
#
# Hlídá se proto přímo VÝSLEDEK (posty po splatnosti), ne živost kontejneru.
#
# Samoopravu dělá jen na frontendu a jen na přesně tenhle podpis (osiřelý
# posluchač na 4200 + lavina restartů). Backendu ani orchestrátoru se nedotýká.
set -uo pipefail

CONF=/srv/homelab/secrets/homelab.conf
STATE=/var/lib/postiz-watchdog
CONTAINER=postiz
DB_CONTAINER=postiz-postgres
OVERDUE_MIN=45          # o kolik minut smí post zaostat, než je to problém
OVERDUE_COUNT=3         # jeden zaseknutý post ještě není výpadek
RESTART_STORM=50        # přírůstek restartů mezi běhy = lavina
ALERT_REPEAT_SEC=21600  # stejný problém připomínat nejvýš po 6 h

mkdir -p "$STATE"

notify() {
  local chat tok
  chat=$(grep -oE '^TELEGRAM_CHAT_ID=.*' "$CONF" 2>/dev/null | cut -d= -f2-)
  tok=$(cat "$(grep -oE '^TELEGRAM_TOKEN_FILE=.*' "$CONF" 2>/dev/null | cut -d= -f2-)" 2>/dev/null)
  [ -n "${tok:-}" ] && [ -n "${chat:-}" ] || return 0
  curl -s -m 20 -X POST "https://api.telegram.org/bot$tok/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"chat_id":"%s","text":%s}' "$chat" \
          "$(printf '%s' "$1" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" \
    >/dev/null || true
}

# Stejný problém neposílá při každém běhu, ale nejvýš jednou za ALERT_REPEAT_SEC.
alert_once() {
  # Pozor: `local a=$1 b="$a"` NEFUNGUJE — bash rozvine všechna slova příkazu
  # dřív, než přiřadí první z nich, takže `$a` je pod `set -u` nedefinované a
  # skript spadne na „unbound variable" ještě před první kontrolou. Proto zvlášť.
  local key=$1
  local msg=$2
  local f="$STATE/alert_$key"
  local now
  now=$(date +%s)
  if [ -f "$f" ] && [ $(( now - $(cat "$f" 2>/dev/null || echo 0) )) -lt "$ALERT_REPEAT_SEC" ]; then
    return 0
  fi
  echo "$now" > "$f"
  notify "$msg"
}
clear_alert() { rm -f "$STATE/alert_$1"; }

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
  alert_once down "🔴 Postiz: kontejner $CONTAINER neběží — nic se nepublikuje."
  exit 1
fi
clear_alert down

# ---------- 1) posty po splatnosti ----------
OVERDUE=$(docker exec -i "$DB_CONTAINER" psql -U postiz -d postiz -tAc \
  "select count(*) from \"Post\" where state='QUEUE' and \"deletedAt\" is null
     and \"publishDate\" < now() - interval '$OVERDUE_MIN minutes';" 2>/dev/null | tr -d ' ')

if [[ "$OVERDUE" =~ ^[0-9]+$ ]] && [ "$OVERDUE" -ge "$OVERDUE_COUNT" ]; then
  OLDEST=$(docker exec -i "$DB_CONTAINER" psql -U postiz -d postiz -tAc \
    "select min(\"publishDate\")::timestamp(0) from \"Post\" where state='QUEUE'
       and \"deletedAt\" is null and \"publishDate\" < now();" 2>/dev/null | tr -d ' ')
  alert_once overdue "🔴 Postiz nepublikuje: $OVERDUE postů po splatnosti (nejstarší $OLDEST).
Zkontroluj: docker exec $CONTAINER pm2 list"
else
  clear_alert overdue
fi

# ---------- 2) lavina restartů pm2 ----------
JLIST=$(timeout 45 docker exec "$CONTAINER" pm2 jlist 2>/dev/null)
if [ -n "$JLIST" ]; then
  while IFS='|' read -r name restarts; do
    [ -n "$name" ] || continue
    prev_f="$STATE/restarts_$name"
    prev=$(cat "$prev_f" 2>/dev/null || echo "$restarts")
    echo "$restarts" > "$prev_f"
    delta=$(( restarts - prev ))
    [ "$delta" -ge "$RESTART_STORM" ] || continue

    # Samooprava jen pro frontend — jen tam víme, že příčinou je osiřelý
    # posluchač na 4200, a jen tam je restart bezpečný. Backend ani
    # orchestrátor se nikdy nesahá: tam by restart mohl rozbít běžící publikaci.
    if [ "$name" = "frontend" ]; then
      cpid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null)
      owner=$(nsenter -t "$cpid" -n ss -tlnp 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
      pm2_pid=$(printf '%s' "$JLIST" | python3 -c \
        'import json,sys;print(next((str(p.get("pid")) for p in json.load(sys.stdin) if p["name"]=="frontend"),""))' 2>/dev/null)
      if [ -n "${owner:-}" ] && [ "$owner" != "${pm2_pid:-x}" ]; then
        docker exec "$CONTAINER" pm2 stop frontend >/dev/null 2>&1
        kill -TERM "$owner" 2>/dev/null; sleep 5; kill -KILL "$owner" 2>/dev/null
        docker exec "$CONTAINER" pm2 start frontend >/dev/null 2>&1
        notify "🔧 Postiz: frontend restartoval ${delta}× za interval kvůli obsazenému portu 4200.
Osiřelý proces $owner ukončen, frontend nastartován znovu."
        continue
      fi
    fi
    alert_once "storm_$name" "⚠️ Postiz: proces '$name' restartoval ${delta}× za interval (celkem $restarts)."
  done < <(printf '%s' "$JLIST" | python3 -c \
      'import json,sys
for p in json.load(sys.stdin):
    print(f"{p[\"name\"]}|{p[\"pm2_env\"].get(\"restart_time\",0)}")' 2>/dev/null)
fi

# ---------- 3) nově selhané posty ----------
ERRORS=$(docker exec -i "$DB_CONTAINER" psql -U postiz -d postiz -tAc \
  "select count(*) from \"Post\" where state='ERROR' and \"deletedAt\" is null
     and \"publishDate\" > now() - interval '2 hours';" 2>/dev/null | tr -d ' ')
if [[ "$ERRORS" =~ ^[0-9]+$ ]] && [ "$ERRORS" -ge 5 ]; then
  alert_once errors "⚠️ Postiz: $ERRORS postů selhalo za poslední 2 h — možný limit ze strany sítě."
else
  clear_alert errors
fi

exit 0
