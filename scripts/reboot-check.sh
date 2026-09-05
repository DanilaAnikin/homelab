#!/usr/bin/env bash
# Kontrola restartu hostitele: co běželo předtím a co se po startu nevrátilo.
#
# Proč to existuje: stroj běží 41 dní a jeho bootovací cesta je tím pádem
# NEOVĚŘENÁ — 116 kontejnerů, 7 Supabase stacků a hromada systemd jednotek
# nabíhá naostro poprvé po měsících změn. Riziko není v samotném restartu, ale
# v tom, že po něm nepůjde odlišit „tohle nenaběhlo" od „tohle bylo rozbité už
# včera". Proto se stav PŘED restartem uloží a po startu se proti němu porovná.
#
#   reboot-check.sh capture   # spustit TĚSNĚ PŘED restartem
#   reboot-check.sh verify    # po startu (dělá se i samo, viz .timer)
#
# `verify` nikdy nekončí chybou kvůli nálezu — hlásí, nehodnotí. Chybou končí
# jen tehdy, když nemá s čím porovnávat.
set -uo pipefail

STATE=/var/lib/homelab-reboot
CONF=/srv/homelab/secrets/homelab.conf
MODE=${1:-verify}

DOCKER=(docker)
[ "$(id -u)" -eq 0 ] || DOCKER=(sudo -n docker)

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

# Jména swarm tasků obsahují id nasazení, které se po restartu změní. Porovnávat
# je doslova by hlásilo 17 falešných ztrát, tak se ořízne na jméno služby.
normalize() { sed -E 's/\.[0-9]+\.[a-z0-9]{20,}$//'; }

snapshot_containers() { "${DOCKER[@]}" ps --format '{{.Names}}' 2>/dev/null | normalize | sort -u; }
snapshot_failed()     { systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | sort -u; }
snapshot_ports()      { ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un; }

case "$MODE" in
  capture)
    mkdir -p "$STATE" 2>/dev/null || { echo "nelze zapisovat do $STATE (spusť pod rootem)"; exit 1; }
    snapshot_containers > "$STATE/containers"
    snapshot_failed     > "$STATE/failed"
    snapshot_ports      > "$STATE/ports"
    date -Is             > "$STATE/captured_at"
    uname -r             > "$STATE/kernel"
    echo "Základ uložen do $STATE:"
    echo "  kontejnerů: $(wc -l < "$STATE/containers")"
    echo "  už rozbitých jednotek: $(wc -l < "$STATE/failed")"
    echo "  naslouchajících portů: $(wc -l < "$STATE/ports")"
    echo "  jádro: $(cat "$STATE/kernel")"
    echo
    echo "Teď můžeš restartovat. Po startu se kontrola spustí sama (reboot-check.timer)."
    ;;

  verify)
    [ -f "$STATE/containers" ] || { echo "není s čím porovnávat — nejdřív 'capture'"; exit 1; }

    missing_c=$(comm -23 "$STATE/containers" <(snapshot_containers))
    # Jednotky, které jsou rozbité TEĎ a nebyly rozbité PŘED restartem.
    new_failed=$(comm -13 "$STATE/failed" <(snapshot_failed))
    missing_p=$(comm -23 "$STATE/ports" <(snapshot_ports))

    old_kernel=$(cat "$STATE/kernel" 2>/dev/null || echo '?')
    now_kernel=$(uname -r)

    out="Kontrola po restartu ($(date '+%d.%m. %H:%M'))"
    out="$out"$'\n'"Jádro: $old_kernel → $now_kernel"
    out="$out"$'\n'"Základ z: $(cat "$STATE/captured_at" 2>/dev/null | cut -c1-16)"
    out="$out"$'\n'

    if [ -z "$missing_c" ] && [ -z "$new_failed" ] && [ -z "$missing_p" ]; then
      out="$out"$'\n'"✅ Vrátilo se všechno. Žádný chybějící kontejner, port ani nová rozbitá jednotka."
    else
      [ -n "$missing_c" ] && out="$out"$'\n'"🔴 Nevrátily se kontejnery:"$'\n'"$(echo "$missing_c" | sed 's/^/  · /')"$'\n'
      [ -n "$new_failed" ] && out="$out"$'\n'"🔴 Nově rozbité jednotky (před restartem byly v pořádku):"$'\n'"$(echo "$new_failed" | sed 's/^/  · /')"$'\n'
      [ -n "$missing_p" ]  && out="$out"$'\n'"⚠️ Nenaslouchá se na portech:"$'\n'"$(echo "$missing_p" | tr '\n' ' ' | sed 's/^/  /')"$'\n'
    fi

    echo "$out"
    notify "$out"
    ;;

  *)
    echo "použití: $0 {capture|verify}"; exit 2;;
esac
