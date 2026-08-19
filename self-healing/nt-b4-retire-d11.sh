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
# Read-only here: the retire step never writes routing. It is consulted so the
# script can refuse to stop a container that public traffic still points at.
LIVE_CONFIG="${NT_TEST_LIVE:-${DYN:-/etc/dokploy/traefik/dynamic}/natetrader.yml}"
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

# The same question, but able to say "I could not tell". `networks_of` ends in
# `|| true`, so a failing `docker inspect` returns an EMPTY LIST — which reads
# identically to "attached to nothing". retire() consumed that twice: it
# reported "already detached" before doing anything, and then asserted
# "detached from every network" afterwards, both from a docker call that never
# answered. A daemon hiccup or a container renamed between the pre-check and
# the retire was enough to certify containment that had not happened.
#
# Exit status here is the answer: 0 = the list is real (possibly empty), 1 =
# docker did not answer and the caller must not treat the empty list as data.
networks_of_strict(){ # <container> -> prints the list, rc 1 if docker failed
  local raw
  raw="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null)" || return 1
  printf '%s' "$raw" | grep -v '^$' || true
  return 0
}

# ── the Auth liveness probe, and why it is not /auth/v1/settings ───────────
# MEASURED against the live host on 2026-08-19:
#     GET https://ntapi.anikin.cz/auth/v1/settings            -> 401
#     GET https://ntapi.anikin.cz/auth/v1/settings  + apikey  -> 200
#     GET https://ntapi.anikin.cz/auth/v1/verify              -> 400
# Kong's `auth-v1` route carries key-auth, so /settings answers 401
# (`www-authenticate: Key`) to an unauthenticated caller. Every probe in these
# scripts asserted `== 200` and sent no key, so against production they would
# all have scored a failure: --cutover would have rolled back a cutover that
# worked, --rollback would have reported ROLLBACK DID NOT RESTORE SERVICE, and
# retire would have refused claiming Auth was down. Not one rehearsal could see
# it, because every stub answers 200 by fixture.
#
# /auth/v1/verify is one of Kong's `auth-v1-open` routes: no key-auth, so the
# request reaches GoTrue, which rejects the missing token with 400. That 400 is
# a stronger liveness signal than a 200 from a gateway plugin — it proves the
# whole chain answered, Traefik -> Kong -> GoTrue. A 404 means the route is
# gone; 5xx or a curl error means Auth is down; both are failures.
AUTH_PROBE_PATH="/auth/v1/verify"
AUTH_PROBE_OK="400"
pre_checks(){
  echo "PRE-CHECKS"

  # WHO is being retired, before anything about its state is asked.
  #
  # $CONTAINER and $BRIDGE are independent environment overrides and were never
  # compared. Every other pre-check verifies that Traefik points AT $BRIDGE —
  # none verified that $CONTAINER is not $BRIDGE. So NT_OLD_DASHBOARD set to
  # the bridge's own name passed all of them, and retire() then stopped the
  # container serving the public site. The same holds for any other container
  # on the host, Traefik included.
  if [[ "$CONTAINER" == "$BRIDGE" ]]; then
    die "refusing to retire '$CONTAINER': that is the BRIDGE, the container currently
       serving public traffic. NT_OLD_DASHBOARD and NT_BRIDGE_CONTAINER must name
       two different containers."
  fi
  ok "the container to retire is not the bridge" "$CONTAINER != $BRIDGE"

  # Existence is asked as its own question. The previous form was
  #   state="$(docker inspect ... || echo absent)"
  # which concatenates whatever the failing command printed with the fallback,
  # so a command that prints AND fails yields "absent\nabsent" — a value that
  # is not equal to "absent" and takes the success branch. A rehearsal caught
  # it: "there is no such container" allowed the retire.
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    ok "old dashboard found" "$CONTAINER ($(docker inspect -f '{{.State.Status}}' "$CONTAINER"))"
  else
    bad "no container named $CONTAINER" "nothing to retire"
  fi

  # It must not be the one serving. Retiring the live backend is an outage.
  if grep -q "$BRIDGE" "$DYN/natetrader.yml" 2>/dev/null; then
    ok "traffic points at the bridge, not at this container"
  else
    bad "Traefik does not point at $BRIDGE" "retiring $CONTAINER would take the site down"
  fi

  # Belt and braces on the same question from the other side. The check above
  # asks "does traffic go to the bridge"; this asks "does any route still go to
  # the container we are about to stop". They differ if the file ever grows a
  # third router, and the second question is the one whose wrong answer takes
  # the site down. It runs AFTER the check above so that "Stage 1 was never
  # applied" keeps its own diagnosis rather than being reported as this.
  if [[ -r "$LIVE_CONFIG" ]] && grep -qE "url:[[:space:]]*\"?http://${CONTAINER}:" "$LIVE_CONFIG"; then
    bad "$LIVE_CONFIG still routes to $CONTAINER" "retiring it would take that route down"
  else
    ok "no live route points at $CONTAINER"
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
  s="$(status "$API_HOST$AUTH_PROBE_PATH")"
  [[ "$s" == "$AUTH_PROBE_OK" ]] && ok "Auth reachable" "$AUTH_PROBE_PATH -> http $s" || bad "Auth" "$AUTH_PROBE_PATH -> http $s (want $AUTH_PROBE_OK)"

  local nets; nets="$(networks_of "$CONTAINER" | tr '\n' ' ')"
  note info "networks it is on" "${nets:-none}"

  echo
  [[ $FAIL -eq 0 ]] || die "$FAIL pre-check(s) failed — nothing was changed"
  echo "pre-checks: $PASS ok, 0 failed"
}

retire(){
  # Record BEFORE changing anything, or --restore has nothing to work from.
  # WRITTEN VIA A TEMPORARY, because `> "$RECORD"` is applied by the shell
  # BEFORE the command runs. The first version of this guard was
  #     if ! networks_of_strict "$CONTAINER" > "$RECORD"; then die ...
  # which truncates $RECORD to zero bytes and only then discovers that docker
  # could not answer — so the guard added to protect the restore record was the
  # thing that destroyed it, and a second --retire after a transient docker
  # failure would leave --restore with an empty list it considers valid.
  # Demonstrated: a two-line record became zero lines before the die fired.
  local nets_tmp
  nets_tmp="$(mktemp "${TMPDIR:-/tmp}/nt-b4-nets.XXXXXX")"
  if ! networks_of_strict "$CONTAINER" > "$nets_tmp"; then
    rm -f "$nets_tmp"
    die "could not read $CONTAINER's networks — refusing to retire a container whose
       attachment state is unknown, because an unreadable list is indistinguishable
       from an empty one and --restore would have nothing to put back.
       $RECORD was NOT modified."
  fi
  mv -f "$nets_tmp" "$RECORD"
  local n; n="$(grep -c . "$RECORD" || true)"
  [[ "$n" -gt 0 ]] && ok "recorded $n network(s) for restore" "$RECORD" \
                   || note info "recorded networks" "none — docker answered, and the list is empty"

  docker stop "$CONTAINER" >/dev/null
  local state; state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  [[ "$state" == "exited" ]] && ok "stopped" "$CONTAINER" || bad "stop left it '$state'"

  # A restart policy would undo this the next time the daemon restarts.
  # Read, act, then RE-READ. This used to be `docker update … || true` followed
  # unconditionally by ok "restart policy cleared", so a failed update asserted
  # the very property the comment above says prevents the container coming back.
  # The read itself also used `|| echo ""`, so a failing inspect took the
  # "nothing to clear" branch — the idiom this file documents at the top as
  # having already failed open once.
  local policy
  if ! policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null)"; then
    bad "could not read the restart policy" "cannot confirm it will stay down"
  elif [[ -n "$policy" && "$policy" != "no" ]]; then
    docker update --restart=no "$CONTAINER" >/dev/null 2>&1 || true
    local after
    if ! after="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null)"; then
      bad "could not re-read the restart policy" "the update was not confirmed"
    elif [[ "$after" == "no" ]]; then
      ok "restart policy cleared" "was '$policy', now '$after'"
    else
      bad "restart policy NOT cleared" "was '$policy', still '$after'"
    fi
  else
    ok "no restart policy to clear" "reads '$policy'"
  fi

  while read -r net; do
    [[ -n "$net" ]] || continue
    docker network disconnect -f "$net" "$CONTAINER" >/dev/null 2>&1 || true
  done < "$RECORD"

  local left
  if ! left="$(networks_of_strict "$CONTAINER" | tr '\n' ' ')"; then
    bad "could not re-read $CONTAINER's networks" "detachment is unconfirmed, not confirmed"
  elif [[ -z "${left// /}" ]]; then
    ok "detached from every network"
  else
    bad "still attached to" "$left"
  fi

  # It must not be resolvable from the edge any more.
  #
  # WITH A POSITIVE CONTROL, because this check's PASSING value is also its
  # failure-to-run value: the old form took the else branch — and asserted
  # "no longer resolves" — whenever `docker run` exited non-zero for ANY
  # reason, including busybox:latest being absent on an offline host. So the
  # prober is first pointed at a name that MUST resolve. If that fails, the
  # prober is broken and neither answer means anything.
  resolves(){ docker run --rm --network dokploy-network busybox:latest nslookup "$1" >/dev/null 2>&1; }
  if ! resolves "$BRIDGE"; then
    bad "the DNS prober is not working" "$BRIDGE does not resolve either — cannot tell detached from unprobeable"
  else
    ok "DNS prober control" "$BRIDGE resolves, so a negative below is a real negative"
    if resolves "$CONTAINER"; then
      bad "$CONTAINER still resolves on dokploy-network"
    else
      ok "$CONTAINER no longer resolves on dokploy-network"
    fi
  fi

  # It still EXISTS. That is the rollback path and it is asserted, not assumed.
  docker inspect "$CONTAINER" >/dev/null 2>&1 \
    && ok "the container still exists — rollback remains available" \
    || bad "the container is GONE" "the rollback path was destroyed"

  # And the site is still up, which is the only thing that makes this safe.
  local s; s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "public dashboard still healthy after retire" "http 200" \
                      || bad "the site broke when the old container was retired" "http $s"
  s="$(status "$API_HOST$AUTH_PROBE_PATH")"
  [[ "$s" == "$AUTH_PROBE_OK" ]] && ok "Auth still reachable" "$AUTH_PROBE_PATH -> http $s" || bad "Auth broke" "$AUTH_PROBE_PATH -> http $s (want $AUTH_PROBE_OK)"
}

restore(){
  # An EMPTY file passes `-f`, and an empty record is exactly what the old
  # truncating guard above used to leave behind. "Refusing to guess" has to mean
  # refusing to reconnect nothing while reporting success.
  [[ -f "$RECORD" ]] || die "no network record at $RECORD — refusing to guess which networks it was on"
  [[ -s "$RECORD" ]] || die "$RECORD is EMPTY — there is nothing to reattach, and reporting
       'reconnected' from an empty record would assert a restore that did not happen"
  while read -r net; do
    [[ -n "$net" ]] || continue
    docker network connect "$net" "$CONTAINER" >/dev/null 2>&1 || true
  done < "$RECORD"
  docker start "$CONTAINER" >/dev/null
  local state; state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  [[ "$state" == "running" ]] && ok "restarted" "$CONTAINER" || bad "start left it '$state'"
  # networks_of (non-strict) returns "" when docker fails, which would report
  # ok "reconnected  none" for a restore that reattached nothing. retire() was
  # converted to the strict reader; this path is the one the whole plan depends
  # on and was left behind.
  local nets
  if ! nets="$(networks_of_strict "$CONTAINER" | tr '\n' ' ')"; then
    bad "could not read $CONTAINER's networks after restore" "the reattachment is unconfirmed"
  elif [[ -z "${nets// /}" ]]; then
    bad "restore reattached NO networks" "the record listed $(grep -c . "$RECORD" || echo 0)"
  else
    ok "reconnected" "$nets"
  fi
  echo
  echo "NOTE: this only brings the container back. Traffic still points at the"
  echo "bridge, and the unwind has an ORDER:"
  echo "    1. nt-b4-stage2-cutover.sh --rollback   (removes the containment boundary)"
  echo "    2. nt-b4-stage1-cutover.sh --rollback   (points traffic back at this container)"
  echo "Running step 2 first restores a pre-Stage-1 file that has no auth-only"
  echo "router and no deny middleware, which reopens the public data plane."
}

case "${1:---check}" in
  --check)   pre_checks ;;
  --retire)  pre_checks; echo; echo "RETIRE"; retire; echo
             echo "retire: $PASS ok, $FAIL failed"
             [[ $FAIL -eq 0 ]] || exit 1 ;;
  --restore) echo "RESTORE"; restore ;;
  *) die "unknown mode: $1" ;;
esac
