#!/usr/bin/env bash
# ============================================================================
# nt-b4-stage1-cutover.sh — point public dashboard traffic at the frozen bridge.
#
# WHAT CHANGES
# ------------
# Exactly one line of Traefik's file provider: the `natetrader-dashboard`
# service's backend URL. Nothing else. Auth, REST, Kong, the database, Storage
# and Realtime are not touched, not restarted, and not reconfigured. The API
# router (`ntapi.anikin.cz` -> Kong) is Stage 2's problem and must be
# byte-identical when this script finishes.
#
# WHY A RENAME
# ------------
# Traefik watches this directory. An in-place edit is observable half-written:
# the watcher can read a truncated file and drop the router entirely, which
# takes the site down for as long as it takes to notice. `mv` within the same
# filesystem is atomic, so the watcher sees either the whole old file or the
# whole new one. Rollback is the same rename in reverse, which is what makes
# the rollback path the *tested* one rather than a different mechanism
# improvised under pressure.
#
# WHAT WOULD MAKE THIS UNSAFE, AND IS THEREFORE CHECKED FIRST
# -----------------------------------------------------------
# Routing traffic to a container that is not serving is an outage you caused.
# So the bridge must already be up, already answering, and already identifying
# itself as the frozen artifact BEFORE the rename. Every one of those is a
# hard precondition, not a warning.
#
# Usage:
#   nt-b4-stage1-cutover.sh --check              pre-checks only, change nothing
#   nt-b4-stage1-cutover.sh --cutover            pre-checks, then the rename
#   nt-b4-stage1-cutover.sh --rollback           put the previous file back
#   nt-b4-stage1-cutover.sh --verify             external verification only
#
# Exit: 0 success. Non-zero otherwise. On a failed post-check --cutover rolls
# back by itself and still exits non-zero.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

# Overridable so the logic can be exercised against a scratch directory. The
# defaults are the production paths and are what runs in anger; nothing here
# changes behaviour when they are left alone. Pointing them elsewhere is how
# nt-b4-stage1-cutover.test.sh rehearses the rename and the rollback without a
# rehearsal being indistinguishable from the real thing.
: "${DYN:=/etc/dokploy/traefik/dynamic}"
: "${BACKUP_DIR:=/var/lib/homelab/b4}"
LIVE="$DYN/natetrader.yml"
DASH_HOST="${NT_DASH_HOST:-https://nate-trader.anikin.cz}"
API_HOST="${NT_API_HOST:-https://ntapi.anikin.cz}"

# The bridge container. It must already be running and healthy.
BRIDGE_CONTAINER="${NT_BRIDGE_CONTAINER:-natetrader-dashboard-bridge}"
BRIDGE_URL="${NT_BRIDGE_URL:-http://natetrader-dashboard-bridge:3000}"
CURRENT_URL="http://natetrader-dashboard:3000"

# The identity the bridge must report. Not a version string — a claim about
# what the artifact *is*, which the image asserts as a compile-time constant.
WANT_ROLE="frozen-containment-bridge"

PASS=0; FAIL=0
note(){ printf '  %-6s %-54s %s\n' "$1" "$2" "${3:-}"; }
ok(){   PASS=$((PASS+1)); note ok   "$1" "${2:-}"; }
bad(){  FAIL=$((FAIL+1)); note FAIL "$1" "${2:-}"; }
die(){  echo; echo "ABORT: $*"; exit 1; }

# ── the containment monitor's expectation flips WITH the change ──────────────
# The monitor (nt-containment-monitor.sh, systemd every 5 min) reads its
# expected state from ${NT_MONITOR_STATE_DIR:-/var/lib/homelab}/nt-containment-expect. Its own header
# says that file "flips atomically with each cutover stage" — but nothing flipped
# it, so after a successful cutover the monitor kept asserting the PRE-cutover
# world and alarmed on every cycle. An alarm that fires because the monitor was
# never told the change happened trains the operator to ignore it, which is the
# one thing a containment monitor must never do.
#
# Written via a temp + atomic rename, so a monitor run mid-flip reads one whole
# state or the other, never half.
flip_expectation(){ # <build_sha> <open|denied> <frozen|thawed>
  local build="$1" rest="$2" freeze="$3"
  local dir="${NT_MONITOR_STATE_DIR:-/var/lib/homelab}" exp
  [[ -d "$dir" ]] || return 0            # no monitor installed here: nothing to flip
  exp="$dir/nt-containment-expect"
  local tmp; tmp="$(mktemp "$dir/.nt-containment-expect.XXXXXX")" || return 0
  {
    printf '# Written by %s at %s. Do not hand-edit during a cutover.\n' "${0##*/}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'EXPECT_BUILD_SHA=%s\n' "$build"
    printf 'EXPECT_REST=%s\n' "$rest"
    printf 'EXPECT_FREEZE=%s\n' "$freeze"
  } > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$exp"
  note ok "monitor expectation updated" "build=${build:0:12} rest=$rest freeze=$freeze"
}

# ── waiting for Traefik, by observing rather than by guessing ───────────────
# `sleep 5` was the only synchronization with the file watcher. Traefik's
# default providersThrottleDuration is 2s and applies AFTER the fsnotify
# debounce, so under load the new routers can be live later than that — and the
# verification that follows then observes the OLD backend, scores a failure,
# and triggers an automatic rollback. That is a second live config flip caused
# by nothing but impatience, followed by a third if the operator retries.
#
# So: poll for the condition, up to a bound, and report how long it took. A
# timeout still falls through to the verification, which is the thing entitled
# to decide — this only stops the decision being taken before the answer could
# possibly be right. The matcher rehearsal already polls; the production
# scripts were the ones still sleeping.
# VALIDATED AT STARTUP, not at the first call site.
#
# The check lived inside wait_for, and in do_cutover that call is six lines
# AFTER the atomic rename — so a junk bound still flipped the live Traefik
# config and only then died, with verify() never running and the automatic
# rollback never firing. That is verbatim the failure the comment inside
# wait_for describes as closed. A bound that cannot be used is knowable before
# anything is touched, so it is checked before anything is touched.
if [[ -n "${NT_B4_WAIT_SECONDS:-}" && ! "${NT_B4_WAIT_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ABORT: NT_B4_WAIT_SECONDS='${NT_B4_WAIT_SECONDS}' is not a positive integer;" >&2
  echo "       refusing to start with an unusable synchronisation bound. Nothing was changed." >&2
  exit 1
fi

wait_for(){ # <seconds> <description> <predicate...>
  # A WALL-CLOCK bound, not an iteration count. Each predicate makes a curl
  # call with --max-time 15, so `wait_for 30` counting iterations could block
  # for ~480s while printing "NOT observed within 30s" — and in do_cutover that
  # window sits between the atomic rename and `verify`, delaying the automatic
  # rollback of a broken public host by up to eight minutes.
  #
  # And the bound is VALIDATED. It used to be `${NT_B4_WAIT_SECONDS:-$1}` fed
  # straight into `(( i < limit ))`: an identifier-like value (`fast`, `auto`)
  # is an unbound variable inside (( )), which under `set -u` kills the shell —
  # on the statement immediately after `mv -f "$staged" "$LIVE"`, so the config
  # is already flipped, verify never runs and the rollback never fires. Junk
  # like `30s` or `0` silently made the loop body run zero times, removing all
  # synchronisation with Traefik's watcher, which is the failure this function
  # was written to remove.
  local limit="${NT_B4_WAIT_SECONDS:-$1}" desc="$2"; shift 2
  if [[ ! "$limit" =~ ^[1-9][0-9]*$ ]]; then
    die "NT_B4_WAIT_SECONDS='${NT_B4_WAIT_SECONDS:-}' is not a positive integer;
       refusing to run with an unusable synchronisation bound"
  fi
  local deadline=$(( SECONDS + limit ))
  local waited
  while (( SECONDS < deadline )); do
    if "$@" >/dev/null 2>&1; then
      waited=$(( SECONDS - (deadline - limit) ))
      note ok "$desc" "observed after ${waited}s"
      return 0
    fi
    sleep 1
  done
  note info "$desc" "NOT observed within ${limit}s — continuing to verification, which decides"
  return 1
}


# Write access, not root specifically. On the host these are the same thing —
# $DYN is root-owned 0755 and $LIVE is 0600 — but "can I write the file I am
# about to replace" is the property that actually has to hold, and asserting
# the property beats asserting a proxy for it.
[[ -d "$DYN" && -w "$DYN" ]] || die "need write access to $DYN (run as root on the host)"
mkdir -p "$BACKUP_DIR"

# ── helpers ─────────────────────────────────────────────────────────────────

# curl that reports the status without letting a non-2xx kill the script, and
# without conflating "server said 000" with "curl could not run".
http_status(){ # <url> [extra curl args...]
  local url="$1"; shift
  local out rc
  out="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$@" "$url" 2>/dev/null)" || rc=$?
  if [[ -n "${rc:-}" && "${rc}" -ne 0 ]]; then echo "curl-error-$rc"; return 0; fi
  echo "$out"
}

http_body(){ # <url> [extra curl args...]  -> body, "" on any failure
  local url="$1"; shift
  curl -sS --max-time 15 "$@" "$url" 2>/dev/null || true
}

# The same request, but able to distinguish a body from a failure.
#
# `http_body` returns "" when curl fails, and an ABSENCE test over "" succeeds:
# the post-rollback check asks whether the bridge's role is gone, and a request
# that never completed contains no role, so a connection reset, a Traefik reload
# window or a stall past --max-time all scored ok "the bridge is no longer
# serving" and the script printed ROLLBACK VERIFIED. An absence test must never
# be run on a result that cannot tell absence from failure.
http_body_ok(){ # <url> [extra curl args...]  -> body on stdout, rc 1 if the request failed
  local url="$1"; shift
  local out
  out="$(curl -sS --fail-with-body --max-time 15 "$@" "$url" 2>/dev/null)" || return 1
  printf '%s' "$out"
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
# ── pre-checks ──────────────────────────────────────────────────────────────
pre_checks(){
  echo "PRE-CHECKS"

  [[ -f "$LIVE" ]] || die "$LIVE does not exist"

  # 1. the file says what we think it says
  if grep -qF "$CURRENT_URL" "$LIVE"; then
    ok "live config points at the current dashboard" "$CURRENT_URL"
  else
    bad "live config does NOT contain the expected current backend" "$CURRENT_URL"
    die "refusing to edit a file whose current state is not the one this script was written for"
  fi

  # 2. exactly one occurrence, or a sed/replace would change more than intended
  local n; n="$(grep -cF "$CURRENT_URL" "$LIVE")"
  if [[ "$n" == "1" ]]; then ok "exactly one occurrence to change"
  else bad "found $n occurrences of the backend URL" "expected 1"; fi

  # 3. the bridge container exists and is running
  local state; state="$(docker inspect -f '{{.State.Status}}' "$BRIDGE_CONTAINER" 2>/dev/null || echo absent)"
  if [[ "$state" == "running" ]]; then ok "bridge container running" "$BRIDGE_CONTAINER"
  else bad "bridge container is '$state'" "$BRIDGE_CONTAINER"; fi

  # 4. it has not been restart-looping
  local restarts; restarts="$(docker inspect -f '{{.RestartCount}}' "$BRIDGE_CONTAINER" 2>/dev/null || echo '?')"
  if [[ "$restarts" == "0" ]]; then ok "bridge restart count" "0"
  else bad "bridge has restarted $restarts times" "it is not stable"; fi

  # 5. it is on the network Traefik will reach it over
  if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$BRIDGE_CONTAINER" 2>/dev/null \
     | grep -q 'dokploy-network'; then
    ok "bridge is attached to dokploy-network"
  else
    bad "bridge is NOT on dokploy-network" "Traefik would get a 502"
  fi

  # 6. it actually answers, from inside the network, before any traffic moves
  local health; health="$(docker run --rm --network dokploy-network curlimages/curl:latest \
      -sS --max-time 10 "$BRIDGE_URL/api/health" 2>/dev/null || true)"
  if [[ -n "$health" ]]; then ok "bridge /api/health answers from dokploy-network"
  else bad "bridge /api/health did not answer from inside the network"; fi

  # 7. and identifies itself as the frozen artifact
  if [[ "$health" == *"\"artifact_role\":\"$WANT_ROLE\""* ]]; then
    ok "bridge declares its identity" "$WANT_ROLE"
  else
    bad "bridge does not declare artifact_role=$WANT_ROLE" "wrong image?"
  fi
  if [[ "$health" == *'"writes_enabled":false'* ]]; then
    ok "bridge declares writes_enabled=false"
  else
    bad "bridge does not declare writes_enabled=false"
  fi

  # 7b. SERVING IS NOT THE SAME AS CONFIGURED.
  #
  # /api/health returns early in the proxy, before any configuration is read,
  # so it answers 200 from an image that cannot serve a single real page. The
  # bridge requires SUPABASE_SERVER_URL — the internal Kong origin — and
  # getSupabaseServerUrl() throws when it is missing, deliberately, so a
  # dashboard cannot silently fall back to the public origin and defeat the
  # containment it exists for.
  #
  # This is not hypothetical. The image currently in production predates that
  # function and its container does not set the variable, so a bridge started
  # from production's environment would answer 200 here and 503 on everything
  # else. A health check that cannot tell those apart is worse than none.
  #
  # A protected API path is the discriminator: 401 means the proxy reached
  # authentication and the configuration is present; 503 means it never got
  # that far.
  local protected; protected="$(docker run --rm --network dokploy-network curlimages/curl:latest \
      -sS -o /dev/null -w '%{http_code}' --max-time 10 "$BRIDGE_URL/api/accounts" 2>/dev/null || echo 000)"
  case "$protected" in
    401) ok "bridge is CONFIGURED, not merely serving" "protected read -> 401" ;;
    503) bad "bridge answers 503 on a protected read" "SUPABASE_SERVER_URL is probably unset — it serves /api/health and nothing else" ;;
    *)   bad "bridge protected read returned $protected" "expected 401" ;;
  esac

  # 8. the site is currently healthy — never start a cutover from a broken base,
  #    or the post-checks cannot tell your change from the pre-existing fault
  local s; s="$(http_status "$DASH_HOST/api/health")"
  if [[ "$s" == "200" ]]; then ok "current public dashboard is healthy" "http 200"
  else bad "current public dashboard is not healthy" "http $s"; fi

  # 9. Auth is up. This is the flow the whole containment plan must preserve.
  s="$(http_status "$API_HOST$AUTH_PROBE_PATH")"
  if [[ "$s" == "$AUTH_PROBE_OK" ]]; then ok "Auth reachable" "$AUTH_PROBE_PATH -> http $s"
  else bad "Auth is not reachable" "$AUTH_PROBE_PATH -> http $s (want $AUTH_PROBE_OK)"; fi

  echo
  [[ $FAIL -eq 0 ]] || die "$FAIL pre-check(s) failed — nothing was changed"
  echo "pre-checks: $PASS ok, 0 failed"
}

# ── the change ──────────────────────────────────────────────────────────────
do_cutover(){
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local backup="$BACKUP_DIR/natetrader.yml.pre-stage1.$stamp"
  local staged="$DYN/.natetrader.yml.staged.$$"

  cp -p "$LIVE" "$backup"
  sha256sum "$backup" > "$backup.sha256"
  echo "$backup" > "$BACKUP_DIR/stage1-rollback-target"
  ok "backup taken" "$backup"

  # Build the new content, then check it, then move it. Never edit in place.
  sed "s|$CURRENT_URL|$BRIDGE_URL|" "$LIVE" > "$staged"
  chmod --reference="$LIVE" "$staged"
  chown --reference="$LIVE" "$staged"

  # The staged file must differ in exactly the way intended.
  if ! grep -qF "$BRIDGE_URL" "$staged"; then
    rm -f "$staged"; die "staged file does not contain the bridge URL — no change made"
  fi
  if grep -qF "$CURRENT_URL" "$staged"; then
    rm -f "$staged"; die "staged file still contains the old URL — no change made"
  fi
  # And in NO other way. One changed line, one added, one removed.
  local dl; dl="$(diff <(cat "$LIVE") <(cat "$staged") | grep -cE '^[<>]' || true)"
  if [[ "$dl" != "2" ]]; then
    rm -f "$staged"; die "staged file differs on $dl lines, expected 2 — no change made"
  fi
  # The API router must be untouched: Stage 1 is the dashboard only.
  if ! diff <(grep -A3 'natetrader-api:' "$LIVE") <(grep -A3 'natetrader-api:' "$staged") >/dev/null; then
    rm -f "$staged"; die "the API router changed — that is Stage 2, not Stage 1"
  fi
  ok "staged file verified" "2 changed lines, API router untouched"

  # atomic
  mv -f "$staged" "$LIVE"
  ok "route changed atomically" "$CURRENT_URL -> $BRIDGE_URL"

  # Traefik's file watcher is not instantaneous. Wait for the BRIDGE to be the
  # thing answering, rather than for a number of seconds to pass.
  serving_bridge(){ [[ "$(http_body "$DASH_HOST/api/health")" == *"\"artifact_role\":\"$WANT_ROLE\""* ]]; }
  wait_for 30 "the bridge is answering on $DASH_HOST" serving_bridge || true
}

# ── external verification ───────────────────────────────────────────────────
# Deliberately from OUTSIDE, over the public name, the way a user arrives.
# Checking the container directly would confirm the container and say nothing
# about the route, which is the only thing that changed.
verify(){
  echo "EXTERNAL VERIFICATION ($DASH_HOST)"
  local before_fail=$FAIL

  local s; s="$(http_status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "GET /api/health" "http 200" || bad "GET /api/health" "http $s"

  local body; body="$(http_body "$DASH_HOST/api/health")"
  [[ "$body" == *"\"artifact_role\":\"$WANT_ROLE\""* ]] \
    && ok "public traffic is served BY THE BRIDGE" "$WANT_ROLE" \
    || bad "public traffic is NOT the bridge" "artifact_role missing"
  [[ "$body" == *'"writes_enabled":false'* ]] \
    && ok "bridge reports writes_enabled=false" \
    || bad "bridge does not report writes_enabled=false"

  # the login page must render — an unreachable login is the outage that matters
  s="$(http_status "$DASH_HOST/login")"
  [[ "$s" == "200" ]] && ok "GET /login renders" "http 200" || bad "GET /login" "http $s"

  # a mutating call must be refused at the edge with the frozen body, from
  # outside, unauthenticated — the property the whole artifact exists for
  s="$(http_status "$DASH_HOST/api/accounts" -X POST -H 'Content-Type: application/json' -d '{}')"
  [[ "$s" == "503" ]] && ok "POST /api/accounts refused" "http 503" || bad "POST /api/accounts" "http $s (expected 503)"

  body="$(http_body "$DASH_HOST/api/accounts" -X POST -H 'Content-Type: application/json' -d '{}')"
  [[ "$body" == *'FROZEN_CONTAINMENT_BRIDGE'* ]] \
    && ok "refusal is the frozen body" \
    || bad "refusal body is not FROZEN_CONTAINMENT_BRIDGE"

  for verb in PUT PATCH DELETE; do
    s="$(http_status "$DASH_HOST/api/accounts" -X "$verb")"
    [[ "$s" == "503" ]] && ok "$verb /api/accounts refused" "http 503" || bad "$verb /api/accounts" "http $s"
  done

  # an authenticated read path must still answer (401 unauthenticated is the
  # correct answer here and proves the route reaches the app, not a 502)
  s="$(http_status "$DASH_HOST/api/accounts")"
  [[ "$s" == "401" ]] && ok "GET /api/accounts reaches the app" "http 401 unauthenticated" \
                      || bad "GET /api/accounts" "http $s (expected 401)"

  # Stage 1 must not have touched the API host at all
  s="$(http_status "$API_HOST$AUTH_PROBE_PATH")"
  [[ "$s" == "$AUTH_PROBE_OK" ]] && ok "Auth still reachable (untouched by Stage 1)" "$AUTH_PROBE_PATH -> http $s" \
                      || bad "Auth" "$AUTH_PROBE_PATH -> http $s (want $AUTH_PROBE_OK)"

  echo
  [[ $FAIL -eq $before_fail ]]
}

# After a rollback the question is NOT "is the bridge serving" — it is "did
# service come back". `verify` asserts the bridge's identity and its 503s,
# which is exactly what rolling back removes, so a correct rollback would have
# reported failure. The Stage 2 rehearsal exposed this; Stage 1's own rehearsal
# had missed it because its stub kept returning the bridge's health body after
# the backend had been pointed away.
verify_service(){
  echo "SERVICE VERIFICATION (post-rollback)"
  local before=$FAIL
  local s
  s="$(http_status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "dashboard /api/health" "http 200" || bad "dashboard /api/health" "http $s"
  s="$(http_status "$DASH_HOST/login")"
  [[ "$s" == "200" ]] && ok "GET /login renders" "http 200" || bad "GET /login" "http $s"
  s="$(http_status "$API_HOST$AUTH_PROBE_PATH")"
  [[ "$s" == "$AUTH_PROBE_OK" ]] && ok "auth reachable" "$AUTH_PROBE_PATH -> http $s" || bad "auth NOT reachable" "$AUTH_PROBE_PATH -> http $s (want $AUTH_PROBE_OK)"

  # THE ASSERTION THAT MAKES THIS A ROLLBACK CHECK RATHER THAN A LIVENESS CHECK.
  # All three probes above are 200 whether the route points at the bridge or at
  # the restored dashboard, so on their own they cannot tell "rolled back" from
  # "the rename landed but Traefik never reloaded". The bridge is the only thing
  # that emits artifact_role; the real dashboard does not. Its ABSENCE is the
  # observable difference, and it costs one request we were already making.
  # Matched on the ROLE VALUE, not on the field name. The production dashboard
  # emits no artifact_role at all — measured against the live host, its health
  # body is {"status":"ok","service":"nate-trader-dashboard",...} — but keying
  # on the bare field name would also fail an honest successor that reported
  # some other role, and the question here is only ever "is the BRIDGE gone".
  local body
  if ! body="$(http_body_ok "$DASH_HOST/api/health")"; then
    bad "could not read $DASH_HOST/api/health" "cannot tell whether the bridge is gone; NOT treating a failed request as absence"
    body=""
  elif [[ "$body" == *"\"artifact_role\":\"$WANT_ROLE\""* ]]; then
    bad "the BRIDGE is still serving $DASH_HOST" "artifact_role is still $WANT_ROLE — the rollback did not take effect"
  else
    ok "the bridge is no longer serving $DASH_HOST" "no $WANT_ROLE in the health body"
  fi
  echo
  [[ $FAIL -eq $before ]]
}

# Stage 1's rollback target is the file as it was BEFORE STAGE 1. Stage 2 keeps
# its own pointer and never updates this one, so restoring this target while
# Stage 2 is live does not undo Stage 1 — it deletes the entire Stage 2
# containment boundary: no auth-only router, no `!PathRegexp` guard, no deny
# middleware, and REST/Storage/Realtime/Functions/GraphQL/pg-meta public again
# on ntapi, in one atomic rename. `verify_service` below could not see it: its
# three probes are all satisfied with the data plane wide open, so the script
# printed ROLLBACK VERIFIED over a reopened data plane.
#
# This is not a hypothetical operator slip. `nt-b4-retire-d11.sh` used to close
# by telling the operator to run exactly this command, and that instruction is
# only ever read in the post-Stage-2 state.
#
# The unwind order is Stage 2 first, then Stage 1. That is what the sequence
# rehearsal does, which is why it never caught this.
stage2_markers_in_live(){ grep -qE 'natetrader-api-auth|natetrader-deny-data-plane' "$LIVE"; }

rollback(){
  if stage2_markers_in_live; then
    # AND CHECK THAT THE STEP WE PRESCRIBE CAN ACTUALLY RUN. Stage 2's rollback
    # dies if its recorded target is missing or fails its own checksum, so
    # telling the operator "run Stage 2's rollback first" without looking can
    # leave BOTH scripted paths refusing, mid-incident, with no next step named.
    local s2t s2ok="yes"
    s2t="$(cat "$BACKUP_DIR/stage2-rollback-target" 2>/dev/null || true)"
    if [[ -z "$s2t" || ! -f "$s2t" ]]; then s2ok="no — no Stage 2 rollback target is recorded"
    elif ! sha256sum -c "$s2t.sha256" >/dev/null 2>&1; then s2ok="no — the Stage 2 backup fails its own checksum"
    fi
    if [[ "$s2ok" != yes ]]; then
      die "the live config still carries the Stage 2 containment boundary, AND
       Stage 2's own rollback cannot run: $s2ok
       Both scripted paths are therefore refusing. Recover by hand, in this order:
         1. find the newest $BACKUP_DIR/natetrader.yml.pre-stage2.* whose .sha256 verifies
         2. cp -p it over $LIVE, preserving mode and owner
         3. confirm https://ntapi.anikin.cz/rest/v1/ is reachable again
         4. then re-run $0 --rollback
       Refusing to restore the pre-Stage-1 file, which would reopen the public data plane."
    fi
    die "the live config still carries the Stage 2 containment boundary.
       Rolling Stage 1 back now would restore the pre-Stage-1 file, which has no
       auth-only router and no deny middleware — the public data plane would be
       open again, and this script's own verification would not notice.
       Unwind in order:
         1. nt-b4-retire-d11.sh --restore        (bring the pre-Stage-1 dashboard
                                                  BACK UP first — Stage 1 rollback
                                                  routes to it, and if it is still
                                                  retired the public site 502s)
         2. nt-b4-stage2-cutover.sh --rollback   (remove the containment boundary)
         3. $0 --rollback                         (point traffic back at it)"
  fi
  local target; target="$(cat "$BACKUP_DIR/stage1-rollback-target" 2>/dev/null || true)"
  [[ -n "$target" && -f "$target" ]] || die "no rollback target recorded — refusing to guess"
  sha256sum -c "$target.sha256" >/dev/null 2>&1 \
    || die "backup $target failed its own checksum — refusing to restore a corrupt file"
  # BEFORE routing to it: the backend the restored file names must be able to
  # serve. The pre-Stage-1 file points at http://natetrader-dashboard:3000, and
  # after --retire that container is stopped and detached — so a rollback that
  # blindly restores it hands Traefik a dead backend and the public site 502s,
  # detected only afterwards by verify_service, after a wait and after the
  # outage. The file's own header makes "do not route to a container that is not
  # serving" a hard precondition for the FORWARD cut; it must hold here too.
  #
  # The backend host is read from the file being restored, resolved on
  # dokploy-network, and required to answer before the rename. NT_SKIP_BACKEND_PROBE=1
  # is the documented escape for a restore where the backend is deliberately
  # elsewhere; it prints a warning rather than silently skipping.
  local backend; backend="$(grep -oE 'http://[a-zA-Z0-9._-]+:[0-9]+' "$target" | grep -v 'supabase-kong' | head -1)"
  if [[ "${NT_SKIP_BACKEND_PROBE:-0}" == 1 ]]; then
    echo "WARNING: NT_SKIP_BACKEND_PROBE=1 — not checking that $backend can serve before routing to it"
  elif [[ -n "$backend" ]]; then
    local bhost bport; bhost="${backend#http://}"; bport="${bhost##*:}"; bhost="${bhost%%:*}"
    if ! docker run --rm --network dokploy-network busybox:latest \
           wget -q -T 5 -O /dev/null "http://${bhost}:${bport}/api/health" 2>/dev/null; then
      die "the rollback target routes to ${backend}, but that backend is not answering on
       dokploy-network — restoring it now would 502 the public site. It is almost
       certainly the retired pre-Stage-1 dashboard; bring it back first:
         nt-b4-retire-d11.sh --restore
       then re-run $0 --rollback. (Set NT_SKIP_BACKEND_PROBE=1 only if the backend
       is deliberately somewhere this host cannot reach.)"
    fi
    echo "backend reachable before routing to it: $backend"
  fi
  local staged="$DYN/.natetrader.yml.rollback.$$"
  cp -p "$target" "$staged"
  chmod --reference="$LIVE" "$staged"; chown --reference="$LIVE" "$staged"
  mv -f "$staged" "$LIVE"
  echo "rolled back to $target"
  # Requires a SUCCESSFUL response that lacks the role — not merely the absence
  # of the role, which every failed request also satisfies.
  not_serving_bridge(){
    local b; b="$(http_body_ok "$DASH_HOST/api/health")" || return 1
    [[ "$b" != *"\"artifact_role\":\"$WANT_ROLE\""* ]]
  }
  wait_for 30 "the bridge has stopped answering on $DASH_HOST" not_serving_bridge || true
}

case "${1:---check}" in
  --check)    pre_checks ;;
  --verify)   verify && echo "VERIFY OK ($PASS ok)" || { echo "VERIFY FAILED ($FAIL failed)"; exit 1; } ;;
  --rollback)
      rollback
      if verify_service; then
        # The pre-Stage-1 dashboard is serving again and is not frozen. Its build
        # is whatever the restored container reports.
        _b="$(http_body "$DASH_HOST/api/health" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("buildSha",""))' 2>/dev/null || true)"
        _r="$( [[ -f ${NT_MONITOR_STATE_DIR:-/var/lib/homelab}/nt-containment-expect ]] && . ${NT_MONITOR_STATE_DIR:-/var/lib/homelab}/nt-containment-expect 2>/dev/null; echo "${EXPECT_REST:-open}" )"
        [[ "$_b" =~ ^[0-9a-f]{40}$ ]] && flip_expectation "$_b" "$_r" thawed || true
        echo "ROLLBACK VERIFIED"
      else echo "ROLLBACK DID NOT RESTORE SERVICE"; exit 1; fi ;;
  --cutover)
      pre_checks
      echo
      echo "CUTOVER"
      do_cutover
      echo
      if verify; then
        # Flip the monitor's expectation to match what is now live: the bridge
        # build (read from the container we just cut over to, the honest source)
        # is frozen. REST is unchanged by Stage 1 and stays whatever it was.
        _b="$(http_body "$DASH_HOST/api/health" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("buildSha",""))' 2>/dev/null || true)"
        _r="$( [[ -f ${NT_MONITOR_STATE_DIR:-/var/lib/homelab}/nt-containment-expect ]] && . ${NT_MONITOR_STATE_DIR:-/var/lib/homelab}/nt-containment-expect 2>/dev/null; echo "${EXPECT_REST:-open}" )"
        [[ "$_b" =~ ^[0-9a-f]{40}$ ]] && flip_expectation "$_b" "$_r" frozen \
          || note info "monitor expectation NOT updated" "could not read a 40-char buildSha from the bridge"
        echo "STAGE 1 COMPLETE — $PASS ok, $FAIL failed"
      else
        echo
        echo "POST-CHECK FAILED — rolling back automatically"
        rollback
        if verify_service; then echo "rolled back, service restored. Stage 1 NOT applied."
        else echo "ROLLED BACK BUT SERVICE IS STILL NOT HEALTHY — ESCALATE"; fi
        exit 1
      fi
      ;;
  *) die "unknown mode: $1" ;;
esac
