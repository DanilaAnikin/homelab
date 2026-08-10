#!/usr/bin/env bash
# ============================================================================
# self-improve.sh — týdenní SELF-IMPROVEMENT meta-agent (sebezlepšující mozek).
# Čte historii incidentů + metriky + zálohy + config drift, hledá OPAKUJÍCÍ SE
# problémy, generuje postmortemy s root-cause, SÁM aplikuje bezpečné preventivní
# fixy (dle CLAUDE.md) a zbytek navrhne. Pošle Telegram digest.
# Doplněk reaktivního self-healingu: ten hasí, tenhle PŘEDCHÁZÍ a učí se.
# ============================================================================
set -uo pipefail
cd /srv/homelab/self-healing
set -a; source /srv/homelab/self-healing/agent.env; set +a
OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
LOG=/srv/homelab/self-healing/self-improve.log

PROMPT='Jsi SELF-IMPROVEMENT meta-agent homelab serveru. Cíl: udělat systém odolnějším NEŽ minulý týden.
Dodržuj železná pravidla z CLAUDE.md. Postupuj:

1) HISTORIE INCIDENTŮ: přečti `tail -n 400 /srv/homelab/self-healing/incidents.log`. Najdi OPAKUJÍCÍ SE
   incidenty a eskalace za poslední týden (stejný monitor/kontejner/alert vícekrát).
2) ZDRAVÍ: `tail -n 200 /srv/homelab/self-healing/health-review.log` + `tail -n 50 /srv/homelab/self-healing/restore-drill.log`.
3) METRIKY & TRENDY: přes `sudo docker exec obs-prometheus wget -qO- "http://localhost:9090/api/v1/query?query=..."`:
   - disk trend: predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 7*24*3600)
   - kontejnery s nejvíc restarty; paměťové tlaky; pg spojení. Firing alerty (/api/v1/alerts).
4) ROOT-CAUSE & POSTMORTEM: pro každý opakující se problém napiš stručný postmortem:
   symptom → příčina → PREVENTIVNÍ fix (ne jen restart, ale proč se to děje).
5) SÁM APLIKUJ jen BEZPEČNÉ preventivní fixy (dle CLAUDE.md): úklid disku (prune bez volumes,
   staré logy), zvýšení rozumných limitů v compose s následným `up -d`, oprava zjevné misconfig,
   restart chronicky unhealthy kontejneru. NIKDY data/volumes/DNS/secrets, NIKDY rizikové bez jistoty.
6) CONFIG DRIFT: zkontroluj, zda klíčové skripty na serveru odpovídají gitu:
   `cd /tmp && rm -rf hl && git clone -q https://github.com/DanilaAnikin/homelab hl 2>/dev/null` a
   `diff -rq hl/self-healing /srv/homelab/self-healing 2>/dev/null | grep -v secrets` — nahlas divergenci.
7) NÁVRHY: co NEJde bezpečně automaticky, vypiš jako konkrétní návrhy (co + proč + riziko).

Zakonči PŘESNĚ řádkem a pak max 12 řádků digestu (emoji ✅ opraveno / ⚠️ návrh / 🔁 opakující se):
=== SELF-IMPROVE DIGEST ==='

if timeout 900 claude -p "$PROMPT" \
    --dangerously-skip-permissions --allowedTools "Bash" --output-format text > "$OUT" 2>&1; then
  :
else
  echo "[self-improve agent selhal/timeout]" >> "$OUT"
fi

{ echo "═══ SELF-IMPROVE $(date -Iseconds) ═══"; cat "$OUT"; echo; } >> "$LOG"

DIGEST=$(awk '/=== SELF-IMPROVE DIGEST ===/{f=1;next} f' "$OUT" | head -c 3500)
[[ -z "$DIGEST" ]] && DIGEST=$(tail -c 1200 "$OUT")
printf '%s\n' "🧠 Týdenní self-improvement homelabu:
$DIGEST" | /srv/homelab/self-healing/notify.sh || true
