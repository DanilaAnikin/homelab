#!/usr/bin/env bash
# ============================================================================
# Rehearsal for the bridge deployment and for retiring the old container.
#
# Unlike the cutover rehearsals, this one is NOT stubbed. It builds a private
# docker network, stands up a stand-in for the current dashboard carrying the
# same variable NAMES production carries, and runs the real deploy script
# against them. The env-copying, the port policy, the network attachment, the
# retire, the detach and the restore all happen for real.
#
# WHAT IT CANNOT COVER, STATED RATHER THAN GLOSSED
# ------------------------------------------------
# `--verify` reaches for a live Supabase: it wants health "ok" and a protected
# read answering 401, and there is no Kong on a scratch network. So the deploy
# path is exercised up to and including "the container is running with the
# right environment and no published ports", and the health assertions are
# left for the host, where nt-b4-deploy-bridge.sh --verify runs against the
# real gateway. Pretending a scratch network could answer for that would be the
# same overclaim the seal script is careful about.
#
# The retire half IS fully covered, because stopping, detaching, resolving and
# restoring are all local docker behaviour.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY="$HERE/../nt-b4-deploy-bridge.sh"
RETIRE="$HERE/../nt-b4-retire-d11.sh"
IMAGE="${NT_TEST_IMAGE:-nt-bridge:38bf4a1126a9}"

SUF="$$"
NET="nt-t-net-$SUF"
SRC="nt-t-old-$SUF"
BRIDGE="nt-t-bridge-$SUF"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-deploy.XXXXXX")"
STATE="$WORK/state"; mkdir -p "$STATE"

cleanup(){
  docker rm -f "$SRC" "$BRIDGE" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

OK=0; BAD=0
note(){ printf '  %-6s %s\n' "$1" "$2"; }
pass(){ OK=$((OK+1)); note ok "$1"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1"; }

[[ -f "$DEPLOY" ]] || { echo "missing $DEPLOY"; exit 1; }
[[ -f "$RETIRE" ]] || { echo "missing $RETIRE"; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "missing image $IMAGE — run the seal first"; exit 1; }

echo "bridge deploy / retire rehearsal"
echo

docker network create "$NET" >/dev/null

# A stand-in for the running dashboard, carrying the same variable NAMES
# production carries — including the two the allowlist must NOT pick up, and
# NOT including SUPABASE_SERVER_URL, exactly as production does not.
docker run -d --name "$SRC" --network "$NET" \
  -e NEXT_PUBLIC_SUPABASE_URL=https://ntapi.example.invalid \
  -e NEXT_PUBLIC_SUPABASE_ANON_KEY=stub-anon \
  -e SUPABASE_SERVICE_ROLE_KEY=stub-service-role \
  -e GITHUB_TOKEN=stub-token -e GITHUB_REPO=stub/repo -e GITHUB_STATE_REF=stub-ref \
  -e ALPACA_API_KEY=stub-alpaca -e ALPACA_SECRET_KEY=stub-alpaca-secret \
  -e DASHBOARD_MAINTENANCE_MODE=on \
  busybox:latest sleep 3600 >/dev/null
pass "stand-in dashboard running with production's variable names"

run_deploy(){ NT_OLD_DASHBOARD="$SRC" NT_BRIDGE_CONTAINER="$BRIDGE" NT_NETWORK="$NET" \
              NT_BRIDGE_TAG="$IMAGE" NT_INTERNAL_SUPABASE_URL="http://natetrader-supabase-kong:8000" \
              bash "$DEPLOY" "$@" >"$WORK/out.txt" 2>&1; }

# ── 1. start ────────────────────────────────────────────────────────────────
if run_deploy --start; then pass "--start succeeds"
else fail "--start failed"; tail -5 "$WORK/out.txt"; fi

env_of(){ docker inspect "$BRIDGE" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null; }

# Guard the guard. Every "X was not carried" below is trivially true when the
# container does not exist, and the first run of this rehearsal reported three
# of them green while --start had failed outright.
if docker inspect "$BRIDGE" >/dev/null 2>&1; then
  pass "the bridge container exists, so the environment checks mean something"
else
  fail "the bridge container does not exist — every environment check below is vacuous"
fi

for v in NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY \
         GITHUB_TOKEN GITHUB_REPO GITHUB_STATE_REF; do
  env_of | grep -q "^${v}=" && pass "carried $v" || fail "did NOT carry $v"
done

env_of | grep -q '^SUPABASE_SERVER_URL=http://natetrader-supabase-kong:8000$' \
  && pass "added SUPABASE_SERVER_URL with the unique container name" \
  || fail "SUPABASE_SERVER_URL is missing or wrong"

# The whole point of an allowlist: things the old container had must NOT arrive
# merely because they were there.
for v in ALPACA_API_KEY ALPACA_SECRET_KEY DASHBOARD_MAINTENANCE_MODE; do
  env_of | grep -q "^${v}=" && fail "$v leaked onto the bridge" || pass "$v was not carried"
done

# never the short alias, whatever else happens
env_of | grep -q '^SUPABASE_SERVER_URL=http://kong:8000$' \
  && fail "SUPABASE_SERVER_URL uses the ambiguous alias 'kong'" \
  || pass "does not use the ambiguous 'kong' alias"

[[ -z "$(docker port "$BRIDGE" 2>/dev/null)" ]] \
  && pass "no ports published" || fail "the bridge publishes ports"

docker inspect "$BRIDGE" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
  | grep -q "$NET" && pass "attached to the expected network" || fail "not on the expected network"

# the secrets must not have been left lying around
LEFT="$(ls /dev/shm/nt-bridge-env.* /run/nt-bridge-env.* "${TMPDIR:-/tmp}"/nt-bridge-env.* 2>/dev/null || true)"
[[ -z "$LEFT" ]] && pass "no env file left behind" || fail "an env file was left: $LEFT"

# ── 2. starting twice must be deliberate ────────────────────────────────────
if run_deploy --start; then fail "--start ran again over an existing container"
else grep -q 'already exists' "$WORK/out.txt" \
       && pass "--start refuses to run over an existing container" \
       || fail "refused, but not because it already exists"; fi

# ── 3. retire: stop, detach, keep ───────────────────────────────────────────
# The retire script's pre-checks read the real Traefik path and the public
# hosts, neither of which exists here, so the retire ACTIONS are exercised
# directly. What is being rehearsed is that stop+detach+restore work and that
# the container survives — the pre-checks are covered on the host.
docker network connect "$NET" "$SRC" >/dev/null 2>&1 || true
NETS_BEFORE="$(docker inspect "$SRC" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')"

NT_OLD_DASHBOARD="$SRC" STATE_DIR="$STATE" bash -c '
  set -e
  source /dev/stdin <<<"$(sed -n "/^networks_of()/,/^}/p" "$1")"
  networks_of "$2" > "$3/d11-networks.txt"
  docker stop "$2" >/dev/null
  docker update --restart=no "$2" >/dev/null 2>&1 || true
  while read -r n; do [ -n "$n" ] && docker network disconnect -f "$n" "$2" >/dev/null 2>&1 || true; done < "$3/d11-networks.txt"
' _ "$RETIRE" "$SRC" "$STATE"

[[ "$(docker inspect -f '{{.State.Status}}' "$SRC")" == "exited" ]] \
  && pass "old container stopped" || fail "old container is not stopped"
[[ -z "$(docker inspect "$SRC" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | tr -d ' ')" ]] \
  && pass "old container detached from every network" || fail "old container is still attached"
docker inspect "$SRC" >/dev/null 2>&1 \
  && pass "old container STILL EXISTS — rollback remains available" \
  || fail "the old container was destroyed"
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$SRC")" == "no" ]] \
  && pass "restart policy cleared" || fail "a restart policy would resurrect it"
[[ -s "$STATE/d11-networks.txt" ]] \
  && pass "networks recorded before the change" || fail "nothing recorded — restore would be a guess"

# ── 4. restore ──────────────────────────────────────────────────────────────
while read -r n; do [[ -n "$n" ]] && docker network connect "$n" "$SRC" >/dev/null 2>&1 || true; done < "$STATE/d11-networks.txt"
docker start "$SRC" >/dev/null
[[ "$(docker inspect -f '{{.State.Status}}' "$SRC")" == "running" ]] \
  && pass "restore brings it back" || fail "restore did not start it"
NETS_AFTER="$(docker inspect "$SRC" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
[[ "$(printf '%s' "$NETS_BEFORE" | tr ' ' '\n' | sort | tr -d '\n')" == \
   "$(printf '%s' "$NETS_AFTER"  | tr ' ' '\n' | sort | tr -d '\n')" ]] \
  && pass "restore reattached exactly the recorded networks" \
  || fail "restore reattached a different set ($NETS_BEFORE -> $NETS_AFTER)"

# ── 5. remove ───────────────────────────────────────────────────────────────
if run_deploy --remove; then pass "--remove succeeds"; else fail "--remove failed"; fi
docker inspect "$BRIDGE" >/dev/null 2>&1 && fail "--remove left the container" \
                                         || pass "--remove deleted only the bridge"
docker inspect "$SRC" >/dev/null 2>&1 && pass "--remove did not touch the old container" \
                                      || fail "--remove destroyed the old container"

# ── C4: --start must not start a bridge whose environment is incomplete ─────
# `bad "not present on $SRC"` only incremented a counter. `docker run -d` then
# executed unconditionally and the branch ended without ever consulting $FAIL,
# so a bridge missing a carried secret started anyway and the operator's shell
# saw exit 0. --verify does not catch it either: a bridge with no GITHUB_TOKEN
# still serves /login, still reports artifact_role and writes_enabled=false and
# still 503s every mutating verb. It fails later, on the read paths it exists
# to serve, by which time Stage 1 has cut public traffic to it.
SRC2="nt-t-old2-$SUF"
docker rm -f "$SRC2" >/dev/null 2>&1 || true
docker run -d --name "$SRC2" --network "$NET" \
  -e NEXT_PUBLIC_SUPABASE_URL=https://ntapi.example.invalid \
  -e NEXT_PUBLIC_SUPABASE_ANON_KEY=stub-anon \
  -e GITHUB_TOKEN=stub-token -e GITHUB_REPO=stub/repo -e GITHUB_STATE_REF=stub-ref \
  busybox:latest sleep 600 >/dev/null   # deliberately NO SUPABASE_SERVICE_ROLE_KEY
BRIDGE2="nt-t-bridge2-$SUF"
docker rm -f "$BRIDGE2" >/dev/null 2>&1 || true
if NT_OLD_DASHBOARD="$SRC2" NT_BRIDGE_CONTAINER="$BRIDGE2" NT_NETWORK="$NET" \
   NT_TEST_IMAGE="$IMAGE" NT_BRIDGE_TAG="$IMAGE" STATE_DIR="$STATE" \
   bash "$DEPLOY" --start >"$WORK/out.txt" 2>&1; then
  fail "C4: --start returned 0 with a carried secret missing"
else
  pass "C4: --start refuses when a carried secret is missing"
fi
docker inspect "$BRIDGE2" >/dev/null 2>&1 \
  && fail "C4: it started the bridge anyway" \
  || pass "C4: no bridge container was created"
grep -qi 'refusing to start' "$WORK/out.txt" \
  && pass "C4: and it said why" \
  || { fail "C4: refused without naming the reason"; tail -3 "$WORK/out.txt"; }
docker rm -f "$SRC2" "$BRIDGE2" >/dev/null 2>&1 || true

# ── C7: --remove must not be able to delete the rollback artifact ───────────
# $BRIDGE is an environment override and was never compared to $SRC, so
# NT_BRIDGE_CONTAINER=<the old dashboard> --remove force-deleted the container
# the entire B4 plan keeps as its rollback path. It is the only unconditional
# deletion in the set.
if NT_OLD_DASHBOARD="$SRC" NT_BRIDGE_CONTAINER="$SRC" NT_NETWORK="$NET" \
   STATE_DIR="$STATE" bash "$DEPLOY" --remove >"$WORK/out.txt" 2>&1; then
  fail "C7: --remove accepted the OLD dashboard as its target"
else
  pass "C7: --remove refuses to delete the old dashboard"
fi
docker inspect "$SRC" >/dev/null 2>&1 \
  && pass "C7: the rollback artifact still exists" \
  || fail "C7: the old dashboard was destroyed by --remove"

echo
echo "deploy/retire rehearsal: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN"; exit 0; } || { echo "REHEARSAL RED"; exit 1; }
