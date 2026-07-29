#!/usr/bin/env bash
# Denní proaktivní health-review: LLM agent projde stav homelabu (read-mostly),
# bezpečně opraví jen triviální třídy a pošle majiteli Telegram digest.
# Chytá pomalu se rozjíždějící problémy (disk trend, cert za 13 dní, stará záloha)
# DŘÍV, než z nich ve 2 ráno bude incident. Doplněk reaktivního self-healingu.
set -uo pipefail
cd /srv/homelab/self-healing
set -a; source /srv/homelab/self-healing/agent.env; set +a
OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
LOG=/srv/homelab/self-healing/health-review.log

PROMPT='Proveď DENNÍ HEALTH-REVIEW homelab serveru. Postupuj podle CLAUDE.md (železná pravidla platí!).

Zkontroluj (převážně READ-ONLY):
1. Disk: `df -h /` — kolik % a volných GB.
2. Kontejnery: `sudo docker ps --format "{{.Names}} {{.Status}}"` — najdi unhealthy nebo často restartované (`sudo docker ps -a --format "{{.Names}} {{.RestartCount}}"` kde >3).
3. Certy hlavních domén (dny do vypršení): freio.cz, www.ripieno.xyz, lokwave.cz — `echo | openssl s_client -servername DOMENA -connect DOMENA:443 2>/dev/null | openssl x509 -noout -enddate`.
4. Poslední záloha na R2 (stáří): `sudo rclone lsl r2:homelab-backups/nightly/$(date +%Y-%m)/ --config /srv/homelab/secrets/rclone.conf | sort -k2 | tail -3`. Pokud nejnovější db_freio_ je starší než 30h → problém.
5. Prometheus firing alerty: `sudo docker exec obs-prometheus wget -qO- http://localhost:9090/api/v1/alerts` (spočítej firing).
6. Postgres spojení: `sudo docker exec supabase-db psql -U postgres -tAc "SELECT count(*) FROM pg_stat_activity"` (a shared-postgres).

BEZPEČNĚ OPRAV jen tyto třídy (jinak jen NAHLAS):
- Disk >80 % → `sudo docker system prune -f` (BEZ --volumes).
- Kontejner ve stavu Exited/čistě spadlý → 1× restart.
Cokoli jiného (cert brzy vyprší, stará záloha, vysoké restart county, firing alert) → jen do digestu jako ⚠️.

Zakonči PŘESNĚ tímto řádkem a pak max 8 řádků souhrnu (každý 1 věta, emoji ✅/⚠️):
=== HEALTH DIGEST ==='

if timeout 600 claude -p "$PROMPT" \
    --dangerously-skip-permissions --allowedTools "Bash" --output-format text > "$OUT" 2>&1; then
  :
else
  echo "[health-review agent selhal/timeout]" >> "$OUT"
fi

{ echo "═══ HEALTH REVIEW $(date -Iseconds) ═══"; cat "$OUT"; echo; } >> "$LOG"

DIGEST=$(awk '/=== HEALTH DIGEST ===/{f=1;next} f' "$OUT" | head -c 3500)
[[ -z "$DIGEST" ]] && DIGEST=$(tail -c 1200 "$OUT")
/srv/homelab/self-healing/notify.sh "📋 Denní health-review homelabu:
$DIGEST" || true
