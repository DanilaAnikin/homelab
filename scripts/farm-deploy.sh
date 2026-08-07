#!/usr/bin/env bash
# =============================================================================
# farm-deploy.sh <project> — "Push to Production" pro agent-farm projekt.
#
# Bezpečný, idempotentní deploy:
#   1) zmerguje otevřené farm/* PR do main (přes GitHub API; konflikty přeskočí),
#   2) aktualizuje prod checkout na origin/main (git-ifikuje, zachová .env/volumes),
#   3) docker compose up -d --build,
#   4) health-check domény; při selhání ROLLBACK na předchozí commit,
#   5) zapíše výsledek do agent-farm DB (event + deploy_requests).
#
# Nic nemaže bez zálohy. DRY_RUN=1 → jen ukáže, co by udělal.
# Registr projektů je dole v resolve_project().
# =============================================================================
set -uo pipefail

PROJECT="${1:?usage: farm-deploy.sh <project> [request_id]}"
REQ_ID="${2:-}"
DRY_RUN="${DRY_RUN:-0}"
FARM_ENV="/srv/homelab/compose/agent-farm/app/.env"
LOG_TAG="[farm-deploy:$PROJECT]"
BACKUP_ROOT="/srv/homelab/backups/deploy"

log(){ echo "$LOG_TAG $*"; }
die(){ echo "$LOG_TAG ✗ $*" >&2; report "failed" "$*"; exit 1; }

# --- DB helper (agent-farm postgres) ------------------------------------------
DBQ(){ sudo docker exec -i agentfarm-supabase-db-1 psql -U postgres -tAc "$1" 2>/dev/null; }
sql_escape(){ printf '%s' "$1" | sed "s/'/''/g"; }

report(){ # status detail  → deploy_requests (když REQ_ID) + vždy event
  local status="$1" detail="$2"
  local esc; esc="$(sql_escape "${detail:0:2000}")"
  if [[ -n "$REQ_ID" ]]; then
    DBQ "update deploy_requests set status='$status', detail='$esc', commit_sha='${DEPLOYED_SHA:-}', finished_at=case when '$status' in ('done','failed') then now() else finished_at end, started_at=coalesce(started_at,now()) where id='$REQ_ID';" >/dev/null || true
  fi
  DBQ "insert into events(project_id, level, type, message) select id, case when '$status'='failed' then 'error' else 'info' end, 'deploy_$status', '$esc' from projects where name='$PROJECT';" >/dev/null || true
}

# --- Registr projektů: repo | prod_dir | compose_file(rel) | compose_project | health_url
resolve_project(){
  case "$1" in
    contentgen)
      DEPLOY_MODE="compose"
      REPO_SLUG="DanilaAnikin/contentgen"
      PROD_DIR="/srv/homelab/compose/contentgen/app"
      COMPOSE_FILE="docker/docker-compose.homelab.yml"
      COMPOSE_PROJECT="contentgen"
      HEALTH_URL="https://contentgen.anikin.cz"
      ;;
    ivanweb)
      # ivanweb běží přes Dokploy (swarm), ne compose. Merge PR → Dokploy autodeploy
      # z main; my jen zmergujeme a ověříme health (nesaháme na cizí deploy mechanismus).
      DEPLOY_MODE="dokploy"
      REPO_SLUG="DanilaAnikin/ivanweb"
      HEALTH_URL="https://ivan.anikin.cz"
      ;;
    *) die "neznámý projekt '$1' (přidej do resolve_project v farm-deploy.sh)";;
  esac
}

# --- GitHub token -------------------------------------------------------------
gh_token(){
  local t
  t="$(sudo grep -m1 -E '^GITHUB_ADMIN_PAT=' "$FARM_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'\r')"
  [[ -n "$t" ]] || die "GITHUB_ADMIN_PAT chybí v $FARM_ENV"
  printf '%s' "$t"
}
gh_api(){ # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  local args=(-s -X "$method" -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}" "https://api.github.com$path"
}

resolve_project "$PROJECT"
DEPLOY_MODE="${DEPLOY_MODE:-compose}"; PROD_DIR="${PROD_DIR:-}"; COMPOSE_FILE="${COMPOSE_FILE:-}"; COMPOSE_PROJECT="${COMPOSE_PROJECT:-}"
GH_TOKEN="$(gh_token)"
report "running" "Deploy zahájen."
log "start (dry_run=$DRY_RUN, mode=$DEPLOY_MODE) repo=$REPO_SLUG prod=${PROD_DIR:-n/a}"

# --- 1) MERGE otevřených farm/* PR do main -----------------------------------
log "hledám otevřené farm/* PR…"
PRS_JSON="$(gh_api GET "/repos/$REPO_SLUG/pulls?state=open&per_page=100")"
PR_NUMS="$(printf '%s' "$PRS_JSON" | python3 -c 'import sys,json
d=json.load(sys.stdin)
for p in d:
    if str(p.get("head",{}).get("ref","")).startswith("farm/"):
        print(p["number"])' 2>/dev/null)"
MERGED=0; SKIPPED=0
for n in $PR_NUMS; do
  if [[ "$DRY_RUN" == "1" ]]; then log "DRY: zmerguju PR #$n"; MERGED=$((MERGED+1)); continue; fi
  # mergeable? GitHub počítá async, dej mu chvíli
  mstate=""
  for i in 1 2 3; do
    mstate="$(gh_api GET "/repos/$REPO_SLUG/pulls/$n" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("mergeable"))' 2>/dev/null)"
    [[ "$mstate" == "None" || -z "$mstate" ]] && sleep 2 || break
  done
  if [[ "$mstate" == "False" ]]; then log "PR #$n není mergeable (konflikt) → přeskakuji"; SKIPPED=$((SKIPPED+1)); continue; fi
  res="$(gh_api PUT "/repos/$REPO_SLUG/pulls/$n/merge" '{"merge_method":"squash"}')"
  if printf '%s' "$res" | grep -q '"merged":true'; then log "PR #$n zmergován ✓"; MERGED=$((MERGED+1))
  else log "PR #$n merge selhal: $(printf '%s' "$res" | head -c 160)"; SKIPPED=$((SKIPPED+1)); fi
done
log "merge hotovo: $MERGED zmergováno, $SKIPPED přeskočeno"

# --- DOKPLOY mode: Dokploy autodeployuje z main po mergi; my jen ověříme health.
if [[ "${DEPLOY_MODE:-compose}" == "dokploy" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then log "DRY: dokploy mode — merge + health-check $HEALTH_URL"; report "done" "DRY-RUN OK ($MERGED PR)"; exit 0; fi
  log "dokploy mode — čekám na autodeploy a ověřuji health ($HEALTH_URL)…"
  ok=0
  for i in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$HEALTH_URL" 2>/dev/null)"
    [[ "$code" =~ ^(200|301|302|307|308|401|403)$ ]] && { ok=1; break; }
    sleep 9
  done
  if [[ "$ok" == "1" ]]; then
    report "done" "Merge $MERGED PR → Dokploy autodeploy. Health OK ✓ up-to-date na produkci."
    log "HOTOVO: $MERGED PR zmergováno, health OK (Dokploy autodeploy)"
  else
    report "failed" "Merge $MERGED PR OK, ale health $HEALTH_URL selhal — zkontroluj Dokploy deploy ručně."
    die "health $HEALTH_URL selhal po mergi (Dokploy autodeploy?)"
  fi
  exit 0
fi

# --- 2) Aktualizace prod checkoutu na origin/main (COMPOSE mode) --------------
[[ -d "$PROD_DIR" ]] || die "prod dir $PROD_DIR neexistuje"
REMOTE_URL="https://x-access-token:${GH_TOKEN}@github.com/${REPO_SLUG}.git"

# git-ifikace (poprvé): init + remote + fetch, BEZ mazání untracked (.env, volumes).
if [[ ! -d "$PROD_DIR/.git" ]]; then
  log "prod není git checkout → git-ifikuji (zachovám untracked soubory)"
  if [[ "$DRY_RUN" != "1" ]]; then
    sudo git -C "$PROD_DIR" init -q
    sudo git -C "$PROD_DIR" remote add origin "$REMOTE_URL" 2>/dev/null || sudo git -C "$PROD_DIR" remote set-url origin "$REMOTE_URL"
  fi
fi

# záloha prod dir (tar, bez node_modules/volumes) PŘED jakoukoliv změnou
TS="$(date +%Y%m%dT%H%M%SZ)"
sudo mkdir -p "$BACKUP_ROOT"
BACKUP_TAR="$BACKUP_ROOT/${PROJECT}-preploy-${TS}.tar.gz"
if [[ "$DRY_RUN" != "1" ]]; then
  sudo tar --exclude='*/node_modules' --exclude='*/.next' --exclude='*/volumes' -czf "$BACKUP_TAR" -C "$(dirname "$PROD_DIR")" "$(basename "$PROD_DIR")" 2>/dev/null && log "záloha: $BACKUP_TAR" || log "varování: záloha se nepovedla"
fi

PREV_SHA=""
if [[ "$DRY_RUN" != "1" ]]; then
  sudo git -C "$PROD_DIR" fetch origin main -q || die "git fetch selhal"
  PREV_SHA="$(sudo git -C "$PROD_DIR" rev-parse HEAD 2>/dev/null || echo '')"
  # reset --hard origin/main: srovná TRACKED soubory na repo; untracked (.env,
  # volumes, homelab compose pokud není v repu) zůstávají netknuté.
  sudo git -C "$PROD_DIR" reset --hard origin/main -q || die "git reset selhal"
  DEPLOYED_SHA="$(sudo git -C "$PROD_DIR" rev-parse --short HEAD)"
  log "prod na origin/main @ $DEPLOYED_SHA (předchozí: ${PREV_SHA:0:7})"
else
  log "DRY: přeskočil bych fetch+reset na origin/main"
fi

# ověř, že compose soubor existuje (jinak by build spadl a nechal prod dole)
[[ "$DRY_RUN" == "1" ]] || [[ -f "$PROD_DIR/$COMPOSE_FILE" ]] || die "compose soubor $COMPOSE_FILE po resetu chybí — NEnasazuji (rollback: tar $BACKUP_TAR)"

# --- 3) Build + up ------------------------------------------------------------
deploy_compose(){ sudo docker compose -p "$COMPOSE_PROJECT" -f "$PROD_DIR/$COMPOSE_FILE" up -d --build 2>&1 | tail -8; }
if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY: docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d --build"
else
  log "compose up --build…"; deploy_compose || die "compose up selhal"
fi

# --- 4) Health check + rollback ----------------------------------------------
health_ok(){
  local code
  for i in $(seq 1 10); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$HEALTH_URL" 2>/dev/null)"
    [[ "$code" =~ ^(200|301|302|307|308|401|403)$ ]] && return 0
    sleep 6
  done
  log "health $HEALTH_URL → poslední kód: $code"; return 1
}
if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY: health-check $HEALTH_URL"; report "done" "DRY-RUN OK ($MERGED PR by se zmergovalo)"; exit 0
fi

if health_ok; then
  log "✓ health OK — $HEALTH_URL"
  report "done" "Nasazeno ✓ up-to-date na produkci ($MERGED PR zmergováno, commit $DEPLOYED_SHA). Health OK."
  log "HOTOVO: up-to-date na produkci ($DEPLOYED_SHA)"
else
  log "✗ health FAIL → ROLLBACK na ${PREV_SHA:0:7}"
  if [[ -n "$PREV_SHA" ]]; then
    sudo git -C "$PROD_DIR" reset --hard "$PREV_SHA" -q
    deploy_compose || true
    if health_ok; then die "deploy selhal (health), rollback na ${PREV_SHA:0:7} obnovil provoz"; fi
  fi
  die "deploy selhal (health) a rollback nezabral — nutný zásah (záloha: $BACKUP_TAR)"
fi
