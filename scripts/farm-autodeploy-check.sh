#!/usr/bin/env bash
# farm-autodeploy-check.sh — auto-deploy on idle. Pro každý projekt s autonomy.autoDeliver=true,
# který DOJEL veškerou práci (0 queued/running/judging tasků) a od posledního úspěšného deploye
# stihl něco dokončit (task_done), zařadí deploy_request. Watcher ho pak nasadí (merge→prod→health→rollback).
# Běží ze systemd timeru á 15 min. Idempotentní: nezaloží druhý, když už deploy běží/čeká.
set -uo pipefail
DBQ(){ sudo docker exec -i agentfarm-supabase-db-1 psql -U postgres -tAc "$1" 2>/dev/null; }

# Whitelist projektů, které JDE nasadit — musí odpovídat resolve_project() ve
# farm-deploy.sh. Druhá nezávislá pojistka vedle autonomy.autoDeliver: kdyby se
# příznak omylem přehodil, u neznámého projektu by deploy stejně jen selhal na
# "neznámý projekt", ale zbytečně by cyklil a plnil deploy_requests.
DEPLOYABLE="contentgen ivanweb"

# Projekty s autoDeliver a BEZ rozdělané práce (idle).
PROJECTS="$(DBQ "select p.name from projects p
  where coalesce((p.autonomy->>'autoDeliver')::boolean,false)=true
    and not exists (select 1 from tasks t where t.project_id=p.id and t.status in ('queued','running','judging'))
  ;" | grep -v '^$' || true)"

for proj in $PROJECTS; do
  case " $DEPLOYABLE " in *" $proj "*) ;; *) continue;; esac
  # BACKOFF po selhání: bez něj se selhavší deploy zkouší každých 15 min donekonečna
  # a KAŽDÝ pokus dojede až k rollback_tar, tzn. rozbalí tar přes produkční adresář
  # a nahodí compose. Po neúspěchu dej 6 h pauzu (a ať si toho někdo všimne).
  failed_recent="$(DBQ "select 1 from deploy_requests where project='$proj' and status='failed'
     and coalesce(finished_at, requested_at) > now() - interval '6 hours' limit 1;")"
  [ -n "$failed_recent" ] && continue
  # už běží/čeká deploy? přeskoč
  inflight="$(DBQ "select 1 from deploy_requests where project='$proj' and status in ('pending','running') limit 1;")"
  [ -n "$inflight" ] && continue
  # nasazuj jen když je CO nasadit: task_done po posledním úspěšném deployi
  recent="$(DBQ "select 1 from events e join projects p on p.id=e.project_id
     where p.name='$proj' and e.type='task_done'
       and e.ts > coalesce((select max(finished_at) from deploy_requests where project='$proj' and status='done'), now()-interval '90 days')
     limit 1;")"
  [ -z "$recent" ] && continue
  DBQ "insert into deploy_requests(project, requested_by) values ('$proj','auto-idle');" >/dev/null
  echo "[$(date -Iseconds)] auto-deploy on idle zařazen: $proj"
done
