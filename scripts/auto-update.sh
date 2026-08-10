#!/usr/bin/env bash
# ============================================================================
# auto-update.sh — týdenní BEZPEČNÝ auto-update kontejnerů s health-gate + rollback.
# Updatuje POUZE contained, non-customer stacky na floating tazích (observability).
# NIKDY pinned prod (supabase/kong/postgres) ani customer data. Při selhání health
# checku po updatu → ROLLBACK na předchozí image (retag) → Telegram report.
# ============================================================================
set -uo pipefail
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/auto-update.log
STACK_DIR=/srv/homelab/compose/observability
PROJECT=observability   # compose project name (docker inspect label)
notify(){ printf '%s\n' "$1" | "$NOTIFY"; }

exec > >(tee -a "$LOG") 2>&1
echo "═══ AUTO-UPDATE $(date -Iseconds) ═══"
cd "$STACK_DIR" || { echo "!! chybí $STACK_DIR"; exit 1; }

# 1) snapshot současných image ID per služba (pro rollback)
declare -A OLD
for c in $(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}'); do
  OLD["$c"]=$(docker inspect --format '{{.Image}}' "$c" 2>/dev/null)
done
echo "  sledovaných kontejnerů: ${#OLD[@]}"

# 2) pull nejnovější + recreate změněné
BEFORE=$(docker images --format '{{.Repository}}:{{.Tag}}@{{.ID}}' | sort)
docker compose pull -q 2>&1 | tail -3
docker compose up -d 2>&1 | tail -3

# 3) health gate — počkej a ověř, že vše běží/healthy
sleep 45
UNHEALTHY=""
for c in "${!OLD[@]}"; do
  st=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}nohealth{{end}}' "$c" 2>/dev/null || echo "missing")
  case "$st" in
    "running healthy"|"running nohealth") : ;;
    *) UNHEALTHY+="$c($st) " ;;
  esac
done

# 4) rollback při selhání
if [[ -n "$UNHEALTHY" ]]; then
  echo "  ✗ NEZDRAVÉ po updatu: $UNHEALTHY → ROLLBACK"
  for c in $UNHEALTHY; do
    name="${c%%(*}"
    old="${OLD[$name]:-}"
    [[ -z "$old" ]] && continue
    # zjisti tag kontejneru a přesměruj ho zpět na starý image ID
    tag=$(docker inspect --format '{{index .Config.Image}}' "$name" 2>/dev/null)
    [[ -n "$tag" ]] && docker tag "$old" "$tag" 2>/dev/null || true
  done
  docker compose up -d 2>&1 | tail -3
  sleep 20
  STILL=""
  for c in "${!OLD[@]}"; do
    st=$(docker inspect --format '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
    [[ "$st" == "running" ]] || STILL+="$c "
  done
  if [[ -n "$STILL" ]]; then
    notify "⚠️ auto-update: rollback observability NEDOKONČEN, stále nezdravé: $STILL — nutný zásah." || true
    exit 1
  fi
  notify "↩️ auto-update: nový image observability byl NEZDRAVÝ ($UNHEALTHY) → rollback na předchozí, běží OK." || true
  exit 0
fi

# 5) report co se aktualizovalo
AFTER=$(docker images --format '{{.Repository}}:{{.Tag}}@{{.ID}}' | sort)
CHANGED=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | grep -vE '@<none>|<none>' | cut -d@ -f1 | sort -u | tr '\n' ' ')
if [[ -n "$CHANGED" ]]; then
  notify "⬆️ auto-update observability OK (health gate prošel). Aktualizováno: $CHANGED" || true
  echo "  aktualizováno: $CHANGED"
else
  echo "  žádné nové image (vše aktuální)"
fi
exit 0
