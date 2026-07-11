#!/usr/bin/env bash
# ============================================================================
# smoke-test.sh — kontrola celého serveru. Spusť: sudo bash smoke-test.sh
# Vše ✔ = server v pořádku. Cokoli ✘ oprav dřív, než nasadíš projekty.
# ============================================================================
PASS=0; FAILN=0
ok()  { echo "  ✔ $1"; PASS=$((PASS+1)); }
bad() { echo "  ✘ $1"; FAILN=$((FAILN+1)); }
check() { local msg=$1; shift; if "$@" &>/dev/null; then ok "$msg"; else bad "$msg"; fi; }

echo "── Systém"
check "Docker běží"                     systemctl is-active docker
check "fail2ban běží"                   systemctl is-active fail2ban
check "UFW aktivní"                     bash -c 'ufw status | grep -q "Status: active"'
check "Auto-updates zapnuté"            bash -c 'grep -q "Unattended-Upgrade \"1\"" /etc/apt/apt.conf.d/20auto-upgrades'
check "Docker log-rotace nastavena"     bash -c 'grep -q max-size /etc/docker/daemon.json'
check "Disk pod 80 %"                   bash -c '[ "$(df --output=pcent / | tail -1 | tr -dc 0-9)" -lt 80 ]'
check "Server nespí (sleep masked)"     bash -c 'systemctl is-enabled sleep.target 2>&1 | grep -q masked'

echo "── Síť a přístup"
check "Tailscale připojený"             bash -c 'tailscale status --json | jq -e ".BackendState == \"Running\"" '
check "cloudflared (tunel) běží"        systemctl is-active cloudflared
check "Traefik poslouchá na :80"        bash -c 'exec 3<>/dev/tcp/127.0.0.1/80'
check "Dokploy panel na :3000"          bash -c 'curl -fsS -o /dev/null http://127.0.0.1:3000'

echo "── Databáze"
check "Postgres kontejner běží"         bash -c 'docker ps --format "{{.Names}}" | grep -qx shared-postgres'
check "Postgres přijímá spojení"        docker exec shared-postgres pg_isready -U postgres
check "PgBouncer přijímá spojení"       docker run --rm --network dokploy-network postgres:17 pg_isready -h shared-pgbouncer -p 6432

echo "── E-mail a monitoring"
check "SMTP služba na :587"             docker run --rm --network dokploy-network postgres:17 bash -c 'exec 3<>/dev/tcp/smtp/587'
check "Uptime Kuma na :3001"            bash -c 'curl -fsS -o /dev/null http://127.0.0.1:3001'

echo "── Zálohy"
check "Backup timer aktivní"            systemctl is-enabled backup.timer
check "USB /mnt/backup připojené"       mountpoint -q /mnt/backup
check "R2 remote nakonfigurovaný"       bash -c 'rclone listremotes | grep -q "^r2:"'
check "R2 dosažitelné"                  rclone lsd r2: --max-depth 1
check "SSD zdravé (SMART)"              bash -c 'smartctl -H /dev/nvme0 | grep -qiE "PASSED|OK"'

echo
echo "═══ Výsledek: $PASS ✔ / $FAILN ✘ ═══"
[[ $FAILN -eq 0 ]] && echo "Server je zdravý. 🚀" || echo "Oprav ✘ položky (viz RUNBOOK.md / docs/)."
exit $FAILN
