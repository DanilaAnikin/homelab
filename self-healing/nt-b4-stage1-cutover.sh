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

http_body(){ # <url> [extra curl args...]
  local url="$1"; shift
  curl -sS --max-time 15 "$@" "$url" 2>/dev/null || true
}

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
  s="$(http_status "$API_HOST/auth/v1/settings")"
  if [[ "$s" == "200" ]]; then ok "Auth /settings reachable" "http 200"
  else bad "Auth /settings is not reachable" "http $s"; fi

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

  # Traefik's file watcher is not instantaneous.
  sleep 5
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
  s="$(http_status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "Auth still reachable (untouched by Stage 1)" "http 200" \
                      || bad "Auth /settings" "http $s"

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
  s="$(http_status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "auth /settings" "http 200" || bad "auth /settings" "http $s"
  echo
  [[ $FAIL -eq $before ]]
}

rollback(){
  local target; target="$(cat "$BACKUP_DIR/stage1-rollback-target" 2>/dev/null || true)"
  [[ -n "$target" && -f "$target" ]] || die "no rollback target recorded — refusing to guess"
  sha256sum -c "$target.sha256" >/dev/null 2>&1 \
    || die "backup $target failed its own checksum — refusing to restore a corrupt file"
  local staged="$DYN/.natetrader.yml.rollback.$$"
  cp -p "$target" "$staged"
  chmod --reference="$LIVE" "$staged"; chown --reference="$LIVE" "$staged"
  mv -f "$staged" "$LIVE"
  echo "rolled back to $target"
  sleep 5
}

case "${1:---check}" in
  --check)    pre_checks ;;
  --verify)   verify && echo "VERIFY OK ($PASS ok)" || { echo "VERIFY FAILED ($FAIL failed)"; exit 1; } ;;
  --rollback) rollback; verify_service && echo "ROLLBACK VERIFIED" || { echo "ROLLBACK DID NOT RESTORE SERVICE"; exit 1; } ;;
  --cutover)
      pre_checks
      echo
      echo "CUTOVER"
      do_cutover
      echo
      if verify; then
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
