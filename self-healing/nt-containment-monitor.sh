#!/usr/bin/env bash
# ============================================================================
# nt-containment-monitor.sh — out-of-band watch on the Nate Trader dashboard
# containment boundary.
#
# WHY IT EXISTS
# -------------
# The containment cutover moves public dashboard traffic to a frozen bridge and
# then denies every Supabase data-plane path except /auth/v1 at the edge. Both
# stages are reversible, but only if somebody notices they broke something. The
# specific failure that must never go unnoticed is Auth breaking: the edge
# overlay is a path matcher, and a matcher that is one character wrong takes
# login down for everyone while /api/health keeps returning 200.
#
# So this runs OUTSIDE the dashboard container, from the host, and probes the
# public surface the way a user would.
#
# EXPECTED STATE IS DATA, NOT CODE
# --------------------------------
# The expectation for the public REST surface changes exactly once, when the
# edge boundary lands. Encoding that in the script would mean editing and
# redeploying the monitor during a cutover, which is the worst possible moment.
# Instead the expectation lives in a state file that flips atomically with the
# overlay, so the monitor is correct on both sides of the change and the moment
# of the flip is a single rename.
#
# Exit: 0 all expectations held. Non-zero otherwise, with exactly one verdict.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

STATE_DIR=/var/lib/homelab
EXPECT="$STATE_DIR/nt-containment-expect"
LAST="$STATE_DIR/nt-containment-last"
LOCK="$STATE_DIR/nt-containment-monitor.lock"
SECRETS=/srv/homelab/secrets
# Optional. A disposable Auth identity with no account rows, no broker
# credentials and no Vault references. Absent is not a pass: the authenticated
# probes report UNKNOWN, and UNKNOWN is a failure of the monitor's coverage.
PROBE_CRED="$SECRETS/nt-containment-probe.txt"

API_HOST="${NT_API_HOST:-https://ntapi.anikin.cz}"
DASH_HOST="${NT_DASH_HOST:-https://nate-trader.anikin.cz}"

PASS=0; FAIL=0; UNKNOWN=0
FIRST=''
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
ok(){   PASS=$((PASS+1)); note ok "$1" "${2:-}"; }
bad(){  FAIL=$((FAIL+1)); [[ -n "$FIRST" ]] || FIRST="$1: ${2:-}"; note FAIL "$1" "${2:-}"; }
unk(){  UNKNOWN=$((UNKNOWN+1)); [[ -n "$FIRST" ]] || FIRST="$1: coverage gap (${2:-})"; note UNKNOWN "$1" "${2:-}"; }

# ── one run at a time, and never forever ────────────────────────────────────
mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
flock -n 9 || { echo "another nt-containment-monitor run holds the lock"; exit 0; }

# The public anon key. It is public by construction — it ships in the browser
# bundle — but it is read from a root-only file rather than embedded, so the
# monitor has one place to rotate.
#
# SENDING IT IS NOT OPTIONAL. Kong answers 401 to any Supabase request without
# an apikey, so a monitor that omits it sees 401 everywhere and cannot tell
# "Traefik denied this before Kong" from "Kong asked for a key". Measured on the
# first run against production: every single data-plane probe returned 401 and
# the entire denial check was therefore worthless. With the key, the pre-cutover
# answer is 200 and the post-cutover answer is a Traefik refusal, and the
# difference is the thing being monitored.
ANON_FILE="$SECRETS/nt-containment-anon.txt"
ANON=''
[[ -s "$ANON_FILE" ]] && ANON="$(tr -d '[:space:]' < "$ANON_FILE")"

CURL=(curl -sS -o /dev/null -m 15 -w '%{http_code}')
akey(){ [[ -n "$ANON" ]] && printf '%s' "apikey: $ANON" || printf 'X-No-Key: 1'; }
code(){ "${CURL[@]}" -H "$(akey)" "$@" 2>/dev/null || echo 000; }
code_nokey(){ "${CURL[@]}" "$@" 2>/dev/null || echo 000; }
body(){ curl -sS -m 15 -H "$(akey)" "$@" 2>/dev/null || true; }

# ── expectations ────────────────────────────────────────────────────────────
# Written by the cutover, read here. Missing file is a FAIL, not a default:
# defaulting would let the monitor silently assert the pre-cutover world after
# the boundary landed.
if [[ ! -s "$EXPECT" ]]; then
  echo "FATAL: no expectation file at $EXPECT — refusing to guess the expected state"
  exit 2
fi
# shellcheck disable=SC1090
EXPECT_BUILD_SHA=''; EXPECT_REST=''; EXPECT_FREEZE=''
. "$EXPECT"
[[ "$EXPECT_BUILD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "FATAL: EXPECT_BUILD_SHA malformed"; exit 2; }
case "$EXPECT_REST" in open|denied) ;; *) echo "FATAL: EXPECT_REST must be open|denied"; exit 2;; esac
case "$EXPECT_FREEZE" in frozen|thawed) ;; *) echo "FATAL: EXPECT_FREEZE must be frozen|thawed"; exit 2;; esac

echo "nt-containment-monitor $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "expect: build=$EXPECT_BUILD_SHA rest=$EXPECT_REST freeze=$EXPECT_FREEZE"
echo

# Without the key every probe below answers 401 and the denial check silently
# measures nothing. That is a coverage failure, not a pass.
if [[ -z "$ANON" ]]; then
  unk "anon key available for data-plane probes" "no key at $ANON_FILE — denial checks cannot distinguish Traefik from Kong"
else
  ok "anon key available for data-plane probes"
fi

# ── 1. Auth must work, and signup must stay off ─────────────────────────────
ntc_settings="$(body "$API_HOST/auth/v1/settings")"
sc="$(code "$API_HOST/auth/v1/settings")"
if [[ "$sc" == 200 ]]; then ok "auth /settings reachable" "http 200"
else bad "auth /settings reachable" "http $sc"; fi

if [[ -n "$ntc_settings" ]]; then
  if grep -q '"disable_signup"[[:space:]]*:[[:space:]]*true' <<<"$ntc_settings"; then
    ok "signup disabled"
  else
    bad "signup disabled" "disable_signup is not true"
  fi
else
  bad "signup disabled" "no settings body to inspect"
fi

# ── 2. the dashboard serves the expected build ──────────────────────────────
health="$(body "$DASH_HOST/api/health")"
hc="$(code "$DASH_HOST/api/health")"
if [[ "$hc" != 200 ]]; then bad "dashboard /api/health" "http $hc"
else
  got_sha="$(python3 -c "
import json,sys
try: print(json.loads(sys.stdin.read()).get('buildSha',''))
except Exception: print('')
" <<<"$health")"
  # `${got_sha:0:12:-none}` is NOT valid bash — substring expansion takes no
  # default. It sat in the failure branch, which had never executed, so the most
  # important check in this monitor produced neither ok nor fail whenever it was
  # supposed to alarm: 20 ok instead of 21, and exit 0. Found only by
  # deliberately corrupting the expectation and watching the alert not arrive.
  # A monitor whose failure branch is broken is worse than no monitor.
  shown="${got_sha:-none}"
  if [[ "$got_sha" == "$EXPECT_BUILD_SHA" ]]; then ok "dashboard BUILD_SHA" "${shown:0:12}"
  else bad "dashboard BUILD_SHA" "serving ${shown:0:12}, expected ${EXPECT_BUILD_SHA:0:12}"; fi
fi

# ── 3. the write freeze ─────────────────────────────────────────────────────
# A mutating request must be refused while frozen. This is the property that
# makes the bridge safe to leave in front of an unmigrated database.
mc="$(code -X PATCH "$DASH_HOST/api/profile")"
if [[ "$EXPECT_FREEZE" == frozen ]]; then
  if [[ "$mc" == 503 ]]; then ok "mutation frozen" "PATCH /api/profile -> 503"
  else bad "mutation frozen" "PATCH /api/profile -> $mc, expected 503"; fi
else
  if [[ "$mc" == 503 ]]; then bad "freeze lifted as expected" "still 503"
  else ok "freeze lifted as expected" "PATCH -> $mc"; fi
fi

# ── 4. the public data plane, per the current expectation ───────────────────
# Every path the edge overlay is supposed to govern. Before the overlay these
# are reachable; after it they must be denied BEFORE Kong. A 502 anywhere means
# Kong is being reached and is unhealthy, which is its own alarm.
declare -A PATHS=(
  [rest_root]=/rest/v1/
  [rest_table]=/rest/v1/accounts
  [rpc]=/rest/v1/rpc/owns_account
  [graphql]=/graphql/v1
  [storage]=/storage/v1/bucket
  [realtime]=/realtime/v1/
  [functions]=/functions/v1
  [pg]=/pg
  [root]=/
)
for name in "${!PATHS[@]}"; do
  c="$(code "$API_HOST${PATHS[$name]}")"
  if [[ "$c" == 502 || "$c" == 503 || "$c" == 504 ]]; then
    bad "public $name" "gateway error http $c"
  elif [[ "$EXPECT_REST" == denied ]]; then
    # 403/404 from Traefik is the intended shape; 200 means it still reaches Kong
    if [[ "$c" == 200 || "$c" == 401 ]]; then
      bad "public $name denied" "http $c — still reaching Kong"
    else ok "public $name denied" "http $c"; fi
  else
    ok "public $name (pre-cutover, not yet denied)" "http $c"
  fi
done

# path-traversal shapes that a sloppy matcher would let through
if [[ "$EXPECT_REST" == denied ]]; then
  for probe in /auth/v1evil //rest/v1/accounts /auth/v1/../rest/v1/accounts /REST/v1/accounts; do
    c="$(code "$API_HOST$probe")"
    if [[ "$c" == 200 || "$c" == 401 ]]; then bad "denied variant $probe" "http $c"
    else ok "denied variant $probe" "http $c"; fi
  done
fi

# ── 5. authenticated probes ─────────────────────────────────────────────────
# UNKNOWN, never PASS, when no disposable identity is configured. A monitor that
# reports green because it did not look is worse than no monitor.
if [[ -s "$PROBE_CRED" ]]; then
  # credentials are read from a root-only file and passed on stdin to curl via
  # --data @-, never on the command line where the process list would show them
  PROBE_EMAIL=''; PROBE_PASSWORD=''; PROBE_ANON=''
  . "$PROBE_CRED"
  tok="$(printf '{"email":"%s","password":"%s"}' "$PROBE_EMAIL" "$PROBE_PASSWORD" \
    | curl -sS -m 20 -X POST "$API_HOST/auth/v1/token?grant_type=password" \
        -H 'Content-Type: application/json' -H "apikey: $PROBE_ANON" --data @- 2>/dev/null \
    | python3 -c "
import json,sys
try: print(json.loads(sys.stdin.read()).get('access_token',''))
except Exception: print('')")"
  if [[ -n "$tok" ]]; then
    ok "authenticated login"
    # NO apikey header here: code() already adds one, and Kong rejects a request
    # carrying two with {"message":"Duplicate API key found"} and a 401. Measured
    # against production — the monitor reported a failing authenticated probe
    # that was entirely its own doing, which is exactly the kind of false alarm
    # that gets a monitor muted.
    uc="$(code "$API_HOST/auth/v1/user" -H "Authorization: Bearer $tok")"
    [[ "$uc" == 200 ]] && ok "authenticated /auth/v1/user" "http 200" || bad "authenticated /auth/v1/user" "http $uc"
    # a protected dashboard read must still work through the bridge
    dc="$(code "$DASH_HOST/api/accounts" -H "Cookie: sb-ntapi-auth-token=$tok")"
    if [[ "$dc" == 200 || "$dc" == 401 ]]; then ok "protected dashboard read reachable" "http $dc"
    else bad "protected dashboard read reachable" "http $dc"; fi
    lc="$(code -X POST "$API_HOST/auth/v1/logout" -H "Authorization: Bearer $tok")"
    [[ "$lc" == 204 || "$lc" == 200 ]] && ok "authenticated logout" "http $lc" \
      || bad "authenticated logout" "http $lc"
    unset tok
  else
    bad "authenticated login" "no access token returned"
  fi
  unset PROBE_EMAIL PROBE_PASSWORD PROBE_ANON
else
  unk "authenticated Auth probe" "no disposable identity at $PROBE_CRED"
  unk "authenticated dashboard read" "no disposable identity"
fi

# ── 6. container churn ──────────────────────────────────────────────────────
for c in natetrader-supabase-auth-1 natetrader-supabase-kong natetrader-dashboard; do
  if ! docker inspect "$c" >/dev/null 2>&1; then bad "container $c present" "absent"; continue; fi
  rc="$(docker inspect "$c" --format '{{.RestartCount}}')"
  started="$(docker inspect "$c" --format '{{.State.StartedAt}}')"
  prev="$(grep -oP "^${c}_restarts=\K.*" "$LAST" 2>/dev/null || echo "$rc")"
  if [[ "$rc" != "$prev" ]]; then bad "container $c stable" "restart count $prev -> $rc"
  else ok "container $c stable" "restarts=$rc since ${started:0:19}"; fi
done

# ── 7. verdict, exactly one ─────────────────────────────────────────────────
{
  echo "checked_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "pass=$PASS fail=$FAIL unknown=$UNKNOWN"
  echo "expect_build=$EXPECT_BUILD_SHA expect_rest=$EXPECT_REST expect_freeze=$EXPECT_FREEZE"
  for c in natetrader-supabase-auth-1 natetrader-supabase-kong natetrader-dashboard; do
    printf '%s_restarts=%s\n' "$c" "$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo absent)"
  done
} > "$LAST.tmp"
mv -f "$LAST.tmp" "$LAST"
chmod 600 "$LAST"

echo
echo "=== $PASS ok, $FAIL failed, $UNKNOWN unknown ==="
if [[ $FAIL -gt 0 || $UNKNOWN -gt 0 ]]; then
  # UNKNOWN counts as a failure on purpose: an uncovered probe is not a green
  # boundary, it is a boundary nobody is watching.
  printf 'NT CONTAINMENT MONITOR FAILED\n%s ok / %s failed / %s unknown\nfirst: %s\n' \
    "$PASS" "$FAIL" "$UNKNOWN" "${FIRST:-none}" \
    | /usr/local/bin/notify.sh 2>/dev/null || true
  echo "CONTAINMENT MONITOR FAILED"
  exit 1
fi
echo "CONTAINMENT MONITOR OK"
exit 0
