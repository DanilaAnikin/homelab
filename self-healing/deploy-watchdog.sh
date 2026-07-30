#!/usr/bin/env bash
# ============================================================================
# deploy-watchdog.sh — post-deploy health gate + AUTO-ROLLBACK pro Dokploy apky.
# Když nedávno updatovaná swarm service spadne (running<desired) → `docker service
# rollback` na předchozí funkční spec. Dedup (2h) proti smyčce; při neúspěchu eskaluj.
# Doplněk self-healingu: tenhle je deterministický a cílený jen na čerstvé deploye.
# ============================================================================
set -uo pipefail
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/deploy-watchdog.log
STATE=/srv/homelab/self-healing/rollback-state
WATCH_WINDOW=1800    # jen services updatované za posledních 30 min (= čerstvý deploy)
REROLL_COOLDOWN=7200 # nerollbackuj stejnou service častěji než 2h (anti-loop)
touch "$STATE" 2>/dev/null || true
NOW=$(date +%s)

# Dokploy/app swarm services (ne infra jako obs-*, supabase-* běží mimo swarm / jinak)
SERVICES=$(docker service ls --format '{{.Name}} {{.Replicas}}' 2>/dev/null | grep -E '^(app-|ripieno-|lokwave-|launchmail-)' || true)

last_roll(){ grep "^$1 " "$STATE" 2>/dev/null | tail -1 | awk '{print $2}'; }
mark_roll(){ echo "$1 $NOW" >> "$STATE"; }

while read -r name repl; do
  [[ -z "$name" ]] && continue
  running="${repl%%/*}"; desired="${repl##*/}"
  [[ "$running" == "$desired" ]] && continue          # zdravé
  # updatováno nedávno? (čerstvý deploy)
  upd=$(docker service inspect "$name" --format '{{.UpdatedAt}}' 2>/dev/null)
  upd_epoch=$(date -d "$upd" +%s 2>/dev/null || echo 0)
  age=$(( NOW - upd_epoch ))
  [[ "$age" -gt "$WATCH_WINDOW" || "$age" -lt 0 ]] && continue   # ne čerstvý deploy → nech na self-healingu
  # má kam rollbacknout?
  hasprev=$(docker service inspect "$name" --format '{{if .PreviousSpec}}1{{else}}0{{end}}' 2>/dev/null)
  [[ "$hasprev" != "1" ]] && continue
  # anti-loop
  lr=$(last_roll "$name"); [[ -n "$lr" && $(( NOW - lr )) -lt "$REROLL_COOLDOWN" ]] && {
    "$NOTIFY" "⚠️ deploy-watchdog: '$name' spadlá i po nedávném rollbacku — nutný zásah (bad previous spec?)." || true
    echo "[$(date -Iseconds)] $name: skip (rollback v cooldownu)" >> "$LOG"; continue; }

  echo "[$(date -Iseconds)] $name: čerstvý deploy ($age s) spadlý ($repl) → ROLLBACK" >> "$LOG"
  if docker service rollback "$name" >/dev/null 2>&1; then
    mark_roll "$name"; sleep 25
    nr=$(docker service ls --format '{{.Name}} {{.Replicas}}' | awk -v n="$name" '$1==n{print $2}')
    r2="${nr%%/*}"; d2="${nr##*/}"
    if [[ "$r2" == "$d2" && "$r2" != "0" ]]; then
      "$NOTIFY" "↩️ deploy-watchdog: '$name' po deployi spadla → AUTO-ROLLBACK na předchozí verzi ÚSPĚŠNÝ ($nr)." || true
    else
      "$NOTIFY" "❌ deploy-watchdog: '$name' rollback NEOBNOVIL zdraví ($nr) — nutný zásah." || true
    fi
  else
    "$NOTIFY" "❌ deploy-watchdog: '$name' spadlá po deployi, rollback SELHAL — nutný zásah." || true
  fi
done <<< "$SERVICES"

# prune staré state (>7 dní)
if [[ -s "$STATE" ]]; then awk -v now="$NOW" '($2+0) > now-604800' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; fi
exit 0
