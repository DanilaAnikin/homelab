#!/usr/bin/env bash
# farm-deploy-watcher.sh — vyzvedne 1 pending deploy_request, atomicky ho označí
# 'running' a spustí farm-deploy.sh <project> <id>. Běží ze systemd timeru á 30 s.
set -uo pipefail
DBQ(){ sudo docker exec -i agentfarm-supabase-db-1 psql -U postgres -tAc "$1" 2>/dev/null; }
CLAIM="with c as (select id, project from deploy_requests where status='pending' order by requested_at limit 1 for update skip locked) update deploy_requests d set status='running', started_at=now() from c where d.id=c.id returning d.id||'|'||d.project;"
# grep -m1 '|' → jen datový řádek (ignoruje případný command-tag "UPDATE 1")
ROW="$(DBQ "$CLAIM" | grep -m1 '|')"
[ -z "$ROW" ] && exit 0
ID="${ROW%%|*}"; PROJECT="${ROW##*|}"
echo "[$(date -Iseconds)] deploy start: $PROJECT ($ID)"
/usr/local/bin/farm-deploy.sh "$PROJECT" "$ID"
