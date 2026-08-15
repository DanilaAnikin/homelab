#!/usr/bin/env bash
# ============================================================================
# nt-b4-retire-d11.sh — take the pre-containment dashboard out of service.
#
# The last step of B4. After Stage 2 is verified and has been observed across
# two monitor intervals, the old `natetrader-dashboard` container — the one
# built from d11bbad8, which can write and was never validated against the
# post-migration schema — is stopped and detached from every network it is on.
#
# IT IS NOT DELETED, AND THAT IS THE POINT
# ----------------------------------------
# A stopped container keeps its filesystem, its configuration and its image, so
# `--restore` puts it back in seconds. Deleting it would trade a reversible
# state for an irreversible one at the exact moment the new arrangement has the
# least operating history behind it. The container is the cheapest rollback
# artifact available and it costs nothing to keep.
#
# WHY DISCONNECT AS WELL AS STOP
# ------------------------------
# Stopping is enough today. Disconnecting is what makes it stay enough: a
# stopped container with a restart policy, or one that something later brings
# up as a dependency, rejoins dokploy-network with its old name and Traefik's
# service discovery can find it again. Detached, it cannot be reached even if
# it starts, which turns "we stopped it" into a property rather than a state
# somebody has to keep true.
#
# The networks it was on are recorded first, so --restore is exact rather than
# a guess about which ones mattered.
#
# Usage:
#   nt-b4-retire-d11.sh --check      report what would happen, change nothing
#   nt-b4-retire-d11.sh --retire     stop and disconnect
#   nt-b4-retire-d11.sh --restore    reconnect and start, from the record
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

CONTAINER="${NT_OLD_DASHBOARD:-natetrader-dashboard}"
BRIDGE="${NT_BRIDGE_CONTAINER:-natetrader-dashboard-bridge}"
: "${STATE_DIR:=/var/lib/homelab/b4}"
RECORD="$STATE_DIR/d11-networks.txt"
DASH_HOST="${NT_DASH_HOST:-https://nate-trader.anikin.cz}"
API_HOST="${NT_API_HOST:-https://ntapi.anikin.cz}"
DYN="${DYN:-/etc/dokploy/traefik/dynamic}"

PASS=0; FAIL=0
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
ok(){   PASS=$((PASS+1)); note ok   "$1" "${2:-}"; }
bad(){  FAIL=$((FAIL+1)); note FAIL "$1" "${2:-}"; }
die(){  echo; echo "ABORT: $*"; exit 1; }

status(){ curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$1" 2>/dev/null || echo 000; }
body(){   curl -sS --max-time 15 "$1" 2>/dev/null || true; }

mkdir -p "$STATE_DIR"

networks_of(){ docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null | grep -v '^$' || true; }

pre_checks(){
  echo "PRE-CHECKS"

  local state; state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo absent)"
  [[ "$state" != absent ]] && ok "old dashboard found" "$CONTAINER ($state)" \
                           || bad "no container named $CONTAINER" "nothing to retire"

  # It must not be the one serving. Retiring the live backend is an outage.
  if grep -q "$BRIDGE" "$DYN/natetrader.yml" 2>/dev/null; then
    ok "traffic points at the bridge, not at this container"
  else
    bad "Traefik does not point at $BRIDGE" "retiring $CONTAINER would take the site down"
  fi

  # Stage 2 must be in place: this is the last step, not a shortcut to it.
  if grep -q 'natetrader-deny-data-plane' "$DYN/natetrader.yml" 2>/dev/null; then
    ok "Stage 2 is in place"
  else
    bad "Stage 2 is NOT in place" "retire is the step after it, not instead of it"
  fi

  # And the bridge must be healthy right now, from outside.
  local s; s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "public dashboard healthy" "http 200" || bad "public dashboard" "http $s"
  local b; b="$(body "$DASH_HOST/api/health")"
  [[ "$b" == *'"artifact_role":"frozen-containment-bridge"'* ]] \
    && ok "the bridge is what is serving" || bad "the bridge is not what is serving"
  s="$(status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "Auth reachable" "http 200" || bad "Auth" "http $s"

  local nets; nets="$(networks_of "$CONTAINER" | tr '\n' ' ')"
  note info "networks it is on" "${nets:-none}"

  echo
  [[ $FAIL -eq 0 ]] || die "$FAIL pre-check(s) failed — nothing was changed"
  echo "pre-checks: $PASS ok, 0 failed"
}

retire(){
  # Record BEFORE changing anything, or --restore has nothing to work from.
  networks_of "$CONTAINER" > "$RECORD"
  local n; n="$(wc -l < "$RECORD")"
  [[ "$n" -gt 0 ]] && ok "recorded $n network(s) for restore" "$RECORD" \
                   || note info "recorded networks" "none — it was already detached"

  docker stop "$CONTAINER" >/dev/null
  local state; state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  [[ "$state" == "exited" ]] && ok "stopped" "$CONTAINER" || bad "stop left it '$state'"

  # A restart policy would undo this the next time the daemon restarts.
  local policy; policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null || echo "")"
  if [[ -n "$policy" && "$policy" != "no" ]]; then
    docker update --restart=no "$CONTAINER" >/dev/null 2>&1 || true
    ok "restart policy cleared" "was '$policy'"
  else
    ok "no restart policy to clear"
  fi

  while read -r net; do
    [[ -n "$net" ]] || continue
    docker network disconnect -f "$net" "$CONTAINER" >/dev/null 2>&1 || true
  done < "$RECORD"

  local left; left="$(networks_of "$CONTAINER" | tr '\n' ' ')"
  [[ -z "${left// /}" ]] && ok "detached from every network" \
                         || bad "still attached to" "$left"

  # It must not be resolvable from the edge any more.
  if docker run --rm --network dokploy-network busybox:latest \
       nslookup "$CONTAINER" >/dev/null 2>&1; then
    bad "$CONTAINER still resolves on dokploy-network"
  else
    ok "$CONTAINER no longer resolves on dokploy-network"
  fi

  # It still EXISTS. That is the rollback path and it is asserted, not assumed.
  docker inspect "$CONTAINER" >/dev/null 2>&1 \
    && ok "the container still exists — rollback remains available" \
    || bad "the container is GONE" "the rollback path was destroyed"

  # And the site is still up, which is the only thing that makes this safe.
  local s; s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "public dashboard still healthy after retire" "http 200" \
                      || bad "the site broke when the old container was retired" "http $s"
  s="$(status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "Auth still reachable" "http 200" || bad "Auth broke" "http $s"
}

restore(){
  [[ -f "$RECORD" ]] || die "no network record at $RECORD — refusing to guess which networks it was on"
  while read -r net; do
    [[ -n "$net" ]] || continue
    docker network connect "$net" "$CONTAINER" >/dev/null 2>&1 || true
  done < "$RECORD"
  docker start "$CONTAINER" >/dev/null
  local state; state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  [[ "$state" == "running" ]] && ok "restarted" "$CONTAINER" || bad "start left it '$state'"
  local nets; nets="$(networks_of "$CONTAINER" | tr '\n' ' ')"
  ok "reconnected" "${nets:-none}"
  echo
  echo "NOTE: this only brings the container back. Traffic still points at the"
  echo "bridge until nt-b4-stage1-cutover.sh --rollback is run."
}

case "${1:---check}" in
  --check)   pre_checks ;;
  --retire)  pre_checks; echo; echo "RETIRE"; retire; echo
             echo "retire: $PASS ok, $FAIL failed"
             [[ $FAIL -eq 0 ]] || exit 1 ;;
  --restore) echo "RESTORE"; restore ;;
  *) die "unknown mode: $1" ;;
esac
