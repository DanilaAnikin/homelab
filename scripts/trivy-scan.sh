#!/usr/bin/env bash
# ============================================================================
# trivy-scan.sh — týdenní sken zranitelností běžících image (CRITICAL/HIGH).
# Trivy běží jako kontejner (aquasec/trivy) s perzistentní cache; report na Telegram.
# Read-only, nic nemění — jen upozorní na CVE + doporučí, které image povýšit.
# ============================================================================
set -uo pipefail
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/trivy-scan.log
CACHE_VOL=trivy-cache
TRIVY="docker run --rm -v $CACHE_VOL:/root/.cache -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest"
notify(){ printf '%s\n' "$1" | "$NOTIFY"; }

docker pull aquasec/trivy:latest >/dev/null 2>&1 || true

# unikátní běžící image (vynech <none> a lokální app buildy bez CVE hodnoty? scanujeme vše)
IMAGES=$(docker ps --format '{{.Image}}' | sort -u | grep -v '^<none>')
REPORT=""; CRIT_TOTAL=0
{ echo "═══ TRIVY $(date -Iseconds) ═══"; } >> "$LOG"

for img in $IMAGES; do
  json=$(timeout 240 $TRIVY image --severity CRITICAL,HIGH --quiet --format json --scanners vuln "$img" 2>/dev/null) || { echo "  $img: sken selhal/timeout" >> "$LOG"; continue; }
  crit=$(echo "$json" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except: print(0); sys.exit()
c=0
for r in d.get('Results') or []:
    for v in r.get('Vulnerabilities') or []:
        if v.get('Severity')=='CRITICAL': c+=1
print(c)" 2>/dev/null || echo 0)
  high=$(echo "$json" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except: print(0); sys.exit()
c=0
for r in d.get('Results') or []:
    for v in r.get('Vulnerabilities') or []:
        if v.get('Severity')=='HIGH': c+=1
print(c)" 2>/dev/null || echo 0)
  echo "  $img: CRITICAL=$crit HIGH=$high" >> "$LOG"
  if [[ "$crit" -gt 0 ]]; then
    REPORT+="🔴 $img: ${crit} CRITICAL, ${high} HIGH"$'\n'
    CRIT_TOTAL=$((CRIT_TOTAL+crit))
  fi
done

if [[ "$CRIT_TOTAL" -gt 0 ]]; then
  notify "🛡️ Trivy sken — nalezeny CRITICAL zranitelnosti (zvaž povýšení image):
$REPORT" || true
else
  notify "🛡️ Trivy sken OK — žádné CRITICAL zranitelnosti v běžících image." || true
fi
echo "  (CRITICAL celkem: $CRIT_TOTAL)" >> "$LOG"
