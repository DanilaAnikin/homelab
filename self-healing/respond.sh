#!/usr/bin/env bash
# Self-healing responder: dostane popis incidentu ($1 nebo stdin), spustí Claude Code headless.
# Po doběhnutí VŽDY notifikuje majitele na Telegram (výsledek: OPRAVENO / ESKALACE / selhal) —
# dřív eskalace končily jen v incidents.log (tiché), viz audit.
set -uo pipefail
INCIDENT="${1:-$(cat)}"
cd /srv/homelab/self-healing
set -a; source /srv/homelab/self-healing/agent.env; set +a
TS=$(date -Iseconds)
LOG=/srv/homelab/self-healing/incidents.log
NOTIFY=/srv/homelab/self-healing/notify.sh
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

{
  echo "═══════════ INCIDENT $TS ═══════════"
  echo "» $INCIDENT"
  echo "─── agent ───"
} >> "$LOG"

if timeout 900 claude -p "INCIDENT: $INCIDENT

Diagnostikuj a BEZPEČNĚ oprav dle CLAUDE.md (železná pravidla dodrž!). Nakonec napiš shrnutí: příčina → akce → výsledek (OPRAVENO nebo ESKALACE:...)." \
  --dangerously-skip-permissions \
  --allowedTools "Bash" \
  --output-format text > "$OUT" 2>&1; then
  cat "$OUT" >> "$LOG"
else
  echo "[agent timeout/selhal]" >> "$OUT"
  echo "[agent timeout/selhal]" >> "$LOG"
fi
echo "═══════════ KONEC $TS ═══════════" >> "$LOG"
echo "" >> "$LOG"

# ── VŽDY notifikuj majitele výsledkem (viditelnost do self-healingu) ──────────
TAIL=$(tail -c 600 "$OUT" | tr '\n' ' ')
if grep -qiE 'ESKALACE:' "$OUT"; then
  ESC=$(grep -iE 'ESKALACE:' "$OUT" | head -1)
  "$NOTIFY" "⚠️ Self-healing ESKALACE — incident: ${INCIDENT} | ${ESC}" || true
elif grep -qiE 'agent timeout/selhal' "$OUT"; then
  "$NOTIFY" "❌ Self-healing agent SELHAL/timeout na incidentu: ${INCIDENT}" || true
elif grep -qiE 'OPRAVENO' "$OUT"; then
  "$NOTIFY" "✅ Self-healing OPRAVIL: ${INCIDENT} | …${TAIL: -300}" || true
else
  "$NOTIFY" "ℹ️ Self-healing doběhl (nejednoznačný výsledek): ${INCIDENT} | …${TAIL: -250}" || true
fi
