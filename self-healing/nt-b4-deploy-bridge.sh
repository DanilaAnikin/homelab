#!/usr/bin/env bash
# ============================================================================
# nt-b4-deploy-bridge.sh — start the frozen bridge alongside production.
#
# Runs ON THE HOST. Loads the sealed image, starts a container on
# dokploy-network under a NEW name, and verifies it. It receives no traffic:
# nothing points at it until nt-b4-stage1-cutover.sh moves the route, which is
# a separate, separately-rehearsed step.
#
# This is additive and reversible. `--remove` deletes the container it created
# and nothing else; production is untouched either way.
#
# SECRETS ARE COPIED, NEVER READ
# ------------------------------
# The bridge needs the same service-role key and GitHub token the current
# dashboard uses, because it still serves reads. Rather than fetch those
# anywhere they could be seen, the environment is copied container-to-container
# on this host with `docker inspect` piped into `docker run` — the values never
# appear in a terminal, a log, an argument list or a file. What IS printed is
# the list of variable NAMES, because "which secrets does this hold" is a
# question that should be answerable without holding them.
#
# WHAT IS DELIBERATELY ADDED, AND WHY IT MATTERS
# ----------------------------------------------
# SUPABASE_SERVER_URL. The bridge reaches Kong over the internal network, and
# getSupabaseServerUrl() THROWS if it is unset — on purpose, so a dashboard
# cannot quietly fall back to the public origin and defeat the containment it
# exists for. The image in production predates that function and its container
# does not set the variable, so copying production's environment alone yields a
# bridge that answers 200 on /api/health and 503 on every real page. Measured:
# GET /api/accounts is 503 without it and 401 with it.
#
# The value is the UNIQUE CONTAINER NAME, never the alias `kong`: six tenants
# share dokploy-network and every one of their gateways answers to `kong`, so
# the short name round-robins between other people's gateways.
#
# WHAT IS DELIBERATELY NOT COPIED
#   ALPACA_API_KEY / ALPACA_SECRET_KEY  the dashboard never reads them; broker
#                                       credentials come from Vault
#   DASHBOARD_MAINTENANCE_MODE          no longer consulted anywhere
#   DASHBOARD_FREEZE_BYPASS_USERS       the bypass machinery is inert
#   BUILD_SHA                           baked into the image at build time
#
# Usage:
#   nt-b4-deploy-bridge.sh --load <image.tar> [--expect-sha256 <digest>]
#   nt-b4-deploy-bridge.sh --start
#   nt-b4-deploy-bridge.sh --verify
#   nt-b4-deploy-bridge.sh --remove
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

SRC="${NT_OLD_DASHBOARD:-natetrader-dashboard}"
BRIDGE="${NT_BRIDGE_CONTAINER:-natetrader-dashboard-bridge}"
NETWORK="${NT_NETWORK:-dokploy-network}"
INTERNAL_URL="${NT_INTERNAL_SUPABASE_URL:-http://natetrader-supabase-kong:8000}"
TAG="${NT_BRIDGE_TAG:-}"

PASS=0; FAIL=0
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
ok(){   PASS=$((PASS+1)); note ok   "$1" "${2:-}"; }
bad(){  FAIL=$((FAIL+1)); note FAIL "$1" "${2:-}"; }
die(){  echo; echo "ABORT: $*"; exit 1; }

# Variables carried over from the running dashboard. Named explicitly: an
# allowlist is the right shape here, because the question "what does the frozen
# artifact hold" must have an answer that does not depend on what somebody
# happened to set on the old container.
CARRY=(
  NEXT_PUBLIC_SUPABASE_URL
  NEXT_PUBLIC_SUPABASE_ANON_KEY
  SUPABASE_SERVICE_ROLE_KEY
  GITHUB_TOKEN
  GITHUB_REPO
  GITHUB_STATE_REF
)

case "${1:---verify}" in
# ── load ────────────────────────────────────────────────────────────────────
--load)
  TAR="${2:?--load needs a path to image.tar}"; shift 2
  EXPECT=""
  [[ "${1:-}" == "--expect-sha256" ]] && { EXPECT="$2"; shift 2; }
  [[ -f "$TAR" ]] || die "$TAR does not exist"

  # The transfer is verified before the daemon is allowed near it. A digest
  # checked after import proves the import, not the transfer.
  GOT="$(sha256sum "$TAR" | cut -d' ' -f1)"
  if [[ -n "$EXPECT" ]]; then
    [[ "$GOT" == "$EXPECT" ]] && ok "transfer digest matches" "${GOT:0:16}..." \
                              || die "DIGEST MISMATCH: expected ${EXPECT:0:16}... got ${GOT:0:16}..."
  else
    note info "no --expect-sha256 given" "${GOT:0:16}... (unverified transfer)"
  fi

  LOADED="$(docker load -i "$TAR" | sed -n 's/^Loaded image: //p' | head -1)"
  [[ -n "$LOADED" ]] && ok "image loaded" "$LOADED" || die "docker load produced no image"
  # The tag file is how --start finds the image, so losing it is not cosmetic:
  # `> … 2>/dev/null || true` dropped it silently when the directory did not
  # exist — nothing in this script created it — and --start then died with a
  # misleading "no image tag".
  mkdir -p /var/lib/homelab/b4 || die "cannot create /var/lib/homelab/b4 to record the image tag"
  printf '%s\n' "$LOADED" > /var/lib/homelab/b4/bridge-tag \
    || die "could not record the image tag; --start would not find $LOADED"
  [[ $FAIL -eq 0 ]] || die "$FAIL problem(s) during load"
  echo "  next: $0 --start"
  ;;

# ── start ───────────────────────────────────────────────────────────────────
--start)
  [[ -z "$TAG" ]] && TAG="$(cat /var/lib/homelab/b4/bridge-tag 2>/dev/null || true)"
  [[ -n "$TAG" ]] || die "no image tag; pass NT_BRIDGE_TAG or run --load first"
  docker image inspect "$TAG" >/dev/null 2>&1 || die "image $TAG is not present"

  docker inspect "$SRC" >/dev/null 2>&1 || die "no running dashboard named $SRC to copy configuration from"
  if docker inspect "$BRIDGE" >/dev/null 2>&1; then
    die "$BRIDGE already exists — run --remove first, deliberately"
  fi

  # Build the env file container-to-container. Mode 0600, deleted immediately.
  #
  # Prefer a tmpfs so the values never touch a disk. /dev/shm first because it
  # is tmpfs and writable without root — /run alone made this untestable
  # anywhere but the host, and a deployment script nobody can rehearse is a
  # deployment script nobody has rehearsed.
  ENVDIR=""
  for d in /dev/shm /run "${TMPDIR:-/tmp}"; do
    [[ -d "$d" && -w "$d" ]] && { ENVDIR="$d"; break; }
  done
  [[ -n "$ENVDIR" ]] || die "no writable directory for the environment file"
  [[ "$ENVDIR" == "${TMPDIR:-/tmp}" ]] && note info "env file on disk, not tmpfs" "$ENVDIR"
  ENVFILE="$(mktemp "$ENVDIR/nt-bridge-env.XXXXXX")"
  chmod 600 "$ENVFILE"
  trap 'rm -f "$ENVFILE"' EXIT
  MISSING=()
  for name in "${CARRY[@]}"; do
    line="$(docker inspect "$SRC" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep "^${name}=" || true)"
    if [[ -n "$line" ]]; then printf '%s\n' "$line" >> "$ENVFILE"; else MISSING+=("$name"); fi
  done
  printf 'SUPABASE_SERVER_URL=%s\n' "$INTERNAL_URL" >> "$ENVFILE"

  ok "carried $(( ${#CARRY[@]} - ${#MISSING[@]} )) variable(s)" "$(printf '%s ' "${CARRY[@]}")"
  [[ ${#MISSING[@]} -eq 0 ]] && ok "nothing expected was missing" \
                             || bad "not present on $SRC" "$(printf '%s ' "${MISSING[@]}")"
  ok "added SUPABASE_SERVER_URL" "$INTERNAL_URL"

  # THE GATE. `bad` above only increments a counter; this branch used to run
  # `docker run -d` regardless and end without ever consulting $FAIL, so a
  # bridge missing a carried secret started anyway and the operator's shell saw
  # exit 0. --verify would not catch it either: a bridge with no GITHUB_TOKEN
  # still serves /login, still reports artifact_role and writes_enabled=false,
  # and still 503s every mutating verb — it fails later, on the read paths it
  # exists to serve, by which time Stage 1 has cut public traffic to it.
  # Only --verify had this line. It belongs wherever a `bad` can be recorded.
  if [[ $FAIL -ne 0 ]]; then
    rm -f "$ENVFILE"
    die "$FAIL problem(s) with the environment — refusing to start $BRIDGE.
       Nothing was started and the env file has been removed."
  fi

  # No published port. Traefik reaches it over the network; publishing would
  # expose a pre-cutover artifact to the host's interfaces.
  docker run -d --name "$BRIDGE" --network "$NETWORK" --restart unless-stopped \
    --env-file "$ENVFILE" "$TAG" >/dev/null
  rm -f "$ENVFILE"
  ok "started" "$BRIDGE on $NETWORK"

  PUBLISHED="$(docker port "$BRIDGE" 2>/dev/null || true)"
  [[ -z "$PUBLISHED" ]] && ok "no published ports" "reachable only inside $NETWORK" \
                        || bad "container publishes ports" "$PUBLISHED"

  # THE GATE BELONGS AT THE END, which is what its own comment says and where
  # it was not. The earlier placement sat before `docker run`, so the published-
  # ports check above — the one the comment beside it calls security-relevant,
  # "publishing would expose a pre-cutover artifact to the host's interfaces" —
  # could record a `bad` and the branch would still hand the operator exit 0.
  # That is the identical shape the gate was added to close. And the branch
  # printed no summary, so a FAIL line scrolled past with nothing tallying it.
  echo
  echo "start: $PASS ok, $FAIL failed"
  [[ $FAIL -eq 0 ]] || exit 1
  echo "  next: $0 --verify"
  ;;

# ── verify ──────────────────────────────────────────────────────────────────
--verify)
  docker inspect "$BRIDGE" >/dev/null 2>&1 || die "$BRIDGE does not exist"
  state="$(docker inspect -f '{{.State.Status}}' "$BRIDGE")"
  [[ "$state" == running ]] && ok "running" || bad "state is '$state'"

  probe(){ docker run --rm --network "$NETWORK" curlimages/curl:latest \
             -sS --max-time 10 "$@" 2>/dev/null || true; }
  code(){ probe -o /dev/null -w '%{http_code}' "$@"; }

  for _ in $(seq 1 60); do
    [[ "$(code "http://$BRIDGE:3000/login")" == "200" ]] && break; sleep 1
  done

  s="$(code "http://$BRIDGE:3000/login")"
  [[ "$s" == "200" ]] && ok "serves" "/login -> 200" || bad "/login" "http $s"

  b="$(probe "http://$BRIDGE:3000/api/health")"
  [[ "$b" == *'"artifact_role":"frozen-containment-bridge"'* ]] \
    && ok "declares the frozen identity" || bad "artifact_role is not the bridge"
  [[ "$b" == *'"writes_enabled":false'* ]] \
    && ok "writes_enabled=false" || bad "writes_enabled is not false"
  [[ "$b" == *'"status":"ok"'* ]] && ok "health status ok" \
    || bad "health is not ok" "$(printf '%s' "$b" | grep -oE '\"status\":\"[a-z]+\"' | head -1) — configuration incomplete?"

  # Configured, not merely serving.
  s="$(code "http://$BRIDGE:3000/api/accounts")"
  [[ "$s" == "401" ]] && ok "protected read reaches auth" "http 401" \
                      || bad "protected read" "http $s (503 means SUPABASE_SERVER_URL is unset)"

  # Frozen, including the path that used to skip the proxy.
  for v in POST PUT PATCH DELETE; do
    s="$(code -X "$v" "http://$BRIDGE:3000/api/accounts")"
    [[ "$s" == "503" ]] && ok "$v /api/accounts refused" "http 503" || bad "$v /api/accounts" "http $s"
  done
  s="$(code -X DELETE "http://$BRIDGE:3000/api/accounts/abc.png")"
  [[ "$s" == "503" ]] && ok "DELETE /api/accounts/abc.png refused" "http 503" \
                      || bad "the extension bypass is back" "http $s"
  b="$(probe -X POST "http://$BRIDGE:3000/api/accounts")"
  [[ "$b" == *FROZEN_CONTAINMENT_BRIDGE* ]] && ok "refusal is the frozen body" \
                                            || bad "refusal body is not the frozen one"

  # It is not serving the public yet, and must not be.
  if grep -q "$BRIDGE" /etc/dokploy/traefik/dynamic/natetrader.yml 2>/dev/null; then
    note info "Traefik already points at the bridge" "Stage 1 has been applied"
  else
    ok "no public traffic yet" "Traefik still points elsewhere"
  fi

  echo
  echo "verify: $PASS ok, $FAIL failed"
  [[ $FAIL -eq 0 ]] || exit 1
  ;;

--remove)
  # The one destructive line here, and $BRIDGE is an environment override. With
  # NT_BRIDGE_CONTAINER set to the OLD dashboard's name this force-deleted the
  # rollback artifact the whole B4 plan rests on. It also reported "was not
  # present" for any failure at all — a daemon that is down, a permission
  # denial — which is a false statement rather than an error.
  [[ "$BRIDGE" != "$SRC" ]] || die "refusing to remove '$BRIDGE': that is \$SRC, the ORIGINAL dashboard
       and the rollback path for this whole plan. NT_BRIDGE_CONTAINER and
       NT_OLD_DASHBOARD must name two different containers."
  if ! docker inspect "$BRIDGE" >/dev/null 2>&1; then
    echo "$BRIDGE is not present — nothing to remove"
  elif docker rm -f "$BRIDGE" >/dev/null 2>&1; then
    echo "removed $BRIDGE"
  else
    die "could not remove $BRIDGE — it exists but docker refused"
  fi
  ;;
*) die "unknown mode: ${1:-}" ;;
esac
