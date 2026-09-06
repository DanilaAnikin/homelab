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
# Session scopes a instance šablonových unitů mají v názvu pořadové číslo, které
# se po restartu změní. Bez normalizace by se každý takový hlásil jako „nově
# rozbitý", ačkoli je to tentýž starý problém pod jiným jménem.
snapshot_failed()     {
  systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' \
    | sed -E 's/^session-[0-9]+\.scope$/session-N.scope/; s/@[0-9-]+\.service$/@N.service/' \
    | sort -u
}
# `comm` porovnává bajtově, takže vstup MUSÍ být seřazený bajtově. `sort -un`
# řadí číselně (8080 < 9), což `comm` rozhodí a nahlásí kaskádu ztrát, které
# nenastaly — a to zrovna při restartu, kvůli kterému tenhle skript existuje.
snapshot_ports()      { ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -u; }

case "$MODE" in
  capture)
    mkdir -p "$STATE" 2>/dev/null
    # `mkdir -p` na existující adresář uspěje i bez práva zápisu do něj, takže
    # sám o sobě nic negarantuje. Rozhoduje až skutečný zápis — jinak by capture
    # pod běžným uživatelem ohlásil úspěch a nechal na místě STARÝ základ, proti
    # kterému by se pak po restartu porovnávalo.
    if ! : > "$STATE/.writetest" 2>/dev/null; then
      echo "do $STATE nejde zapisovat — spusť pod rootem (sudo)"; exit 1
    fi
    rm -f "$STATE/.writetest"
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
    for need in containers failed ports captured_at kernel; do
      [ -s "$STATE/$need" ] || {
        echo "základ je neúplný (chybí nebo je prázdný $need) — nejdřív 'capture'"; exit 1; }
    done

    # Stáří základu se HLÁSÍ, ale nikdy kvůli němu neodmítáme porovnat.
    #
    # Původní verze odmítala základ starší než den. Jenže „starší než poslední
    # start" platí po restartu VŽDY (základ se pořizuje před ním), takže z toho
    # zbylo prosté „starší než 24 h" — a základ pořízený den před plánovaným
    # restartem je úplně normální případ. Přesně to se 6. 9. stalo: základ byl
    # o 40 minut za prahem, kontrola odmítla a po jediném restartu, kvůli kterému
    # celý skript vznikl, nedala žádnou informaci. Odmítnout srovnání je horší než
    # srovnat proti staršímu základu a říct, jak je starý — to druhé si čtenář umí
    # zvážit, z prvního nemá nic.
    cap_epoch=$(date -d "$(cat "$STATE/captured_at")" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    age_h=$(( (now_epoch - cap_epoch) / 3600 ))
    stale_note=""
    if [ "$cap_epoch" -gt 0 ] && [ "$age_h" -ge 24 ]; then
      stale_note="⚠️ Základ je ${age_h} h starý — část rozdílů může být běžný provoz, ne následek restartu."
    fi

    missing_c=$(comm -23 "$STATE/containers" <(snapshot_containers))
    # Jednotky, které jsou rozbité TEĎ a nebyly rozbité PŘED restartem.
    new_failed=$(comm -13 "$STATE/failed" <(snapshot_failed))
    missing_p=$(comm -23 "$STATE/ports" <(snapshot_ports))

    old_kernel=$(cat "$STATE/kernel" 2>/dev/null || echo '?')
    now_kernel=$(uname -r)

    out="Kontrola po restartu ($(date '+%d.%m. %H:%M'))"
    out="$out"$'\n'"Jádro: $old_kernel → $now_kernel"
    out="$out"$'\n'"Základ z: $(cat "$STATE/captured_at" 2>/dev/null | cut -c1-16) (stáří ${age_h} h)"
    [ -n "$stale_note" ] && out="$out"$'\n'"$stale_note"
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
