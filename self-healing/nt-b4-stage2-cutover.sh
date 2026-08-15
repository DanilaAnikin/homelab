#!/usr/bin/env bash
# ============================================================================
# nt-b4-stage2-cutover.sh — deny the public Supabase data plane at the edge.
#
# Stage 1 moved dashboard traffic to the frozen bridge. Stage 2 is the
# containment boundary itself: everything ntapi.anikin.cz exposes — REST,
# Storage, Realtime, Functions, GraphQL, the pg meta API, the Kong root —
# stops in front of Kong, and only GoTrue continues.
#
# WHAT IS AND IS NOT TOUCHED
# --------------------------
# One file: Traefik's natetrader.yml. Auth, REST, Kong, the database, Storage
# and Realtime are not recreated, not restarted, and not reconfigured. Kong
# still receives /auth/v1 exactly as before; what changes is what reaches it.
# The dashboard router is carried through unchanged from whatever Stage 1 left,
# and that is verified rather than assumed.
#
# THE FAILURE THAT MUST NOT GO UNNOTICED
# --------------------------------------
# This is a path matcher, and a matcher one character wrong takes login down
# for everyone while /api/health keeps returning 200. So the post-checks lead
# with a real end-to-end sign-in, not with a liveness probe, and the rollback
# is the same atomic rename Stage 1 uses — already rehearsed, not improvised.
#
# The matcher's behaviour under traversal and percent-encoding was measured
# against the production Traefik version before any of this ran; see
# tests/nt-b4-stage2-matcher.test.sh, which found two live bypasses in the
# first draft of the overlay.
#
# Usage:
#   nt-b4-stage2-cutover.sh --check      pre-checks only, change nothing
#   nt-b4-stage2-cutover.sh --cutover    pre-checks, apply, verify, auto-rollback
#   nt-b4-stage2-cutover.sh --verify     external verification only
#   nt-b4-stage2-cutover.sh --rollback   put the previous file back
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${DYN:=/etc/dokploy/traefik/dynamic}"
: "${BACKUP_DIR:=/var/lib/homelab/b4}"
: "${OVERLAY:=$HERE/nt-b4-stage2-overlay.yml}"
LIVE="$DYN/natetrader.yml"
API_HOST="${NT_API_HOST:-https://ntapi.anikin.cz}"
DASH_HOST="${NT_DASH_HOST:-https://nate-trader.anikin.cz}"
# A disposable Auth identity with no account rows and no Vault references.
PROBE_CRED="${NT_PROBE_CRED:-/srv/homelab/secrets/nt-containment-probe.txt}"
ANON_FILE="${NT_ANON_FILE:-/srv/homelab/secrets/nt-anon-key.txt}"

PASS=0; FAIL=0
note(){ printf '  %-6s %-54s %s\n' "$1" "$2" "${3:-}"; }
ok(){   PASS=$((PASS+1)); note ok   "$1" "${2:-}"; }
bad(){  FAIL=$((FAIL+1)); note FAIL "$1" "${2:-}"; }
die(){  echo; echo "ABORT: $*"; exit 1; }

[[ -d "$DYN" && -w "$DYN" ]] || die "need write access to $DYN (run as root on the host)"
[[ -f "$OVERLAY" ]] || die "missing overlay: $OVERLAY"
mkdir -p "$BACKUP_DIR"

status(){ # <url> [curl args...]
  local url="$1"; shift
  local out rc=0
  out="$(curl -sS --path-as-is -o /dev/null -w '%{http_code}' --max-time 15 "$@" "$url" 2>/dev/null)" || rc=$?
  [[ $rc -ne 0 ]] && { echo "curl-error-$rc"; return 0; }
  echo "$out"
}
body(){ local url="$1"; shift; curl -sS --path-as-is --max-time 15 "$@" "$url" 2>/dev/null || true; }

anon_key(){ [[ -r "$ANON_FILE" ]] && tr -d '\r\n' < "$ANON_FILE" || echo ""; }

# ── pre-checks ──────────────────────────────────────────────────────────────
pre_checks(){
  echo "PRE-CHECKS"
  [[ -f "$LIVE" ]] || die "$LIVE does not exist"

  # Stage 1 must already be in place. Applying Stage 2 while public traffic
  # still points at the unfrozen dashboard would deny the data plane in front
  # of an image that is allowed to write, which is the wrong order to fail in.
  if grep -q 'natetrader-dashboard-bridge' "$LIVE"; then
    ok "Stage 1 is in place" "dashboard points at the bridge"
  else
    bad "Stage 1 is NOT in place" "run --cutover on stage 1 first"
  fi

  # The overlay must not already be applied — a second run would back up the
  # already-contained config as the rollback target.
  if grep -q 'natetrader-deny-data-plane' "$LIVE"; then
    bad "the overlay is ALREADY applied" "refusing to re-apply over itself"
  else
    ok "the overlay is not yet applied"
  fi

  # Auth must be healthy BEFORE, or the post-checks cannot tell this change
  # from a fault that was already there.
  local s; s="$(status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "Auth /settings healthy before" "http 200" \
                      || bad "Auth /settings is not healthy" "http $s"

  # And the data plane must currently be REACHABLE. If it is already denied by
  # something else, every post-check below would pass without this change
  # doing anything.
  local key; key="$(anon_key)"
  if [[ -z "$key" ]]; then
    bad "no anon key at $ANON_FILE" "the denial checks would be indistinguishable from 401"
  else
    s="$(status "$API_HOST/rest/v1/" -H "apikey: $key")"
    if [[ "$s" == "403" ]]; then
      bad "/rest/v1 already returns 403" "cannot attribute the denial to this change"
    else
      ok "data plane currently reachable" "/rest/v1 -> http $s"
    fi
  fi

  # The bridge must be serving, because Stage 2 must not be the change that
  # first reveals a broken Stage 1.
  s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "dashboard healthy before" "http 200" \
                      || bad "dashboard is not healthy" "http $s"

  # An authenticated probe identity, or the authenticated half of the matrix
  # reports UNKNOWN — and UNKNOWN is a coverage failure, not a pass.
  [[ -r "$PROBE_CRED" ]] && ok "probe identity available" \
                         || bad "no probe identity at $PROBE_CRED" "the authenticated matrix cannot run"

  echo
  [[ $FAIL -eq 0 ]] || die "$FAIL pre-check(s) failed — nothing was changed"
  echo "pre-checks: $PASS ok, 0 failed"
}

# ── apply ───────────────────────────────────────────────────────────────────
do_cutover(){
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local backup="$BACKUP_DIR/natetrader.yml.pre-stage2.$stamp"
  local staged="$DYN/.natetrader.yml.staged.$$"

  cp -p "$LIVE" "$backup"
  sha256sum "$backup" > "$backup.sha256"
  echo "$backup" > "$BACKUP_DIR/stage2-rollback-target"
  ok "backup taken" "$backup"

  # The overlay is the source of truth for the RULES; the service backends are
  # carried over from the live file so Stage 2 cannot silently revert Stage 1.
  local bridge_url
  bridge_url="$(grep -oE 'http://natetrader-dashboard[^"]*' "$LIVE" | head -1)"
  [[ -n "$bridge_url" ]] || { die "cannot read the current dashboard backend out of $LIVE"; }
  sed "s|http://natetrader-dashboard:3000|$bridge_url|" "$OVERLAY" > "$staged"
  chmod --reference="$LIVE" "$staged"; chown --reference="$LIVE" "$staged"

  # The properties that make this the intended change and not another one.
  grep -q 'natetrader-deny-data-plane' "$staged" || { rm -f "$staged"; die "staged file has no deny middleware"; }
  grep -q '!PathRegexp(`%`)' "$staged" || { rm -f "$staged"; die "staged file lacks the percent guard — the encoded-slash bypass would be open"; }
  grep -qF "$bridge_url" "$staged" || { rm -f "$staged"; die "staged file lost the Stage 1 bridge backend"; }
  grep -q 'http://natetrader-supabase-kong:8000' "$staged" || { rm -f "$staged"; die "staged file lost the Kong service"; }
  ok "staged file verified" "deny middleware, percent guard, bridge backend, Kong service"

  mv -f "$staged" "$LIVE"
  ok "overlay applied atomically"
  sleep 5
}

# ── external verification ───────────────────────────────────────────────────
verify(){
  echo "EXTERNAL VERIFICATION"
  local before=$FAIL
  local key; key="$(anon_key)"

  # AUTH FIRST. This is the flow that must not break, so it is the first thing
  # checked and the first thing a failure rolls back.
  local s; s="$(status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "auth /settings" "http 200" || bad "auth /settings" "http $s"

  if [[ -r "$PROBE_CRED" ]]; then
    local email pw tok
    email="$(sed -n '1p' "$PROBE_CRED")"; pw="$(sed -n '2p' "$PROBE_CRED")"
    tok="$(body "$API_HOST/auth/v1/token?grant_type=password" \
            -H "apikey: $key" -H 'Content-Type: application/json' \
            -d "{\"email\":\"$email\",\"password\":\"$pw\"}" \
          | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
    if [[ -n "$tok" ]]; then
      ok "authenticated sign-in still works"
      s="$(status "$API_HOST/auth/v1/user" -H "apikey: $key" -H "Authorization: Bearer $tok")"
      [[ "$s" == "200" ]] && ok "authenticated /auth/v1/user" "http 200" || bad "authenticated /auth/v1/user" "http $s"
      s="$(status "$API_HOST/auth/v1/logout" -X POST -H "apikey: $key" -H "Authorization: Bearer $tok")"
      [[ "$s" == "204" ]] && ok "authenticated logout" "http 204" || bad "authenticated logout" "http $s"
    else
      bad "sign-in FAILED through the new edge" "this is the failure Stage 2 must never cause"
    fi
  else
    bad "no probe identity" "the authenticated half of the matrix did not run — that is a coverage failure, not a pass"
  fi

  # The data plane, anonymous and authenticated. Both, because a rule that
  # denies only unauthenticated traffic is not containment.
  for path in / /rest/v1/ /rest/v1/accounts /storage/v1/object/public/x \
              /realtime/v1/websocket /functions/v1/x /graphql/v1 /pg/; do
    s="$(status "$API_HOST$path" -H "apikey: $key")"
    [[ "$s" == "403" ]] && ok "anon DENY $path" "http 403" || bad "anon DENY $path" "http $s (expected 403)"
  done

  # The encoded forms that defeated the first draft of the overlay.
  for path in '/auth/v1/../rest/v1/accounts' '/auth/v1/..%2Frest%2Fv1%2Faccounts' \
              '/auth/v1/%252e%252e/rest/v1/accounts' '/auth/v1x'; do
    s="$(status "$API_HOST$path" -H "apikey: $key")"
    [[ "$s" == "403" ]] && ok "anon DENY $path" "http 403" || bad "anon DENY $path" "http $s (expected 403)"
  done

  # The dashboard is on a different host and must be untouched by all of this.
  s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "dashboard unaffected" "http 200" || bad "dashboard" "http $s"
  local b; b="$(body "$DASH_HOST/api/health")"
  [[ "$b" == *'"artifact_role":"frozen-containment-bridge"'* ]] \
    && ok "dashboard is still the bridge" || bad "dashboard is not the bridge any more"

  echo
  [[ $FAIL -eq $before ]]
}

# After a rollback the question is NOT "is the change present" — it is "did
# service come back". Running the post-cutover verification here asserted the
# denials that rollback exists to remove, so a perfectly good rollback reported
# failure. The mode's own message always said "ROLLBACK DID NOT RESTORE
# SERVICE"; the implementation just did not check that.
verify_service(){
  echo "SERVICE VERIFICATION (post-rollback)"
  local before=$FAIL
  local key; key="$(anon_key)"
  local s
  s="$(status "$API_HOST/auth/v1/settings")"
  [[ "$s" == "200" ]] && ok "auth /settings" "http 200" || bad "auth /settings" "http $s"
  s="$(status "$DASH_HOST/api/health")"
  [[ "$s" == "200" ]] && ok "dashboard /api/health" "http 200" || bad "dashboard /api/health" "http $s"
  # the data plane should be reachable again; that is what was undone
  s="$(status "$API_HOST/rest/v1/" -H "apikey: $key")"
  [[ "$s" == "403" ]] && bad "/rest/v1 still denied after rollback" "the overlay may still be live" \
                      || ok "data plane reachable again" "/rest/v1 -> http $s"
  echo
  [[ $FAIL -eq $before ]]
}

rollback(){
  local target; target="$(cat "$BACKUP_DIR/stage2-rollback-target" 2>/dev/null || true)"
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
      pre_checks; echo; echo "CUTOVER"; do_cutover; echo
      if verify; then
        echo "STAGE 2 COMPLETE — $PASS ok, $FAIL failed"
      else
        echo; echo "POST-CHECK FAILED — rolling back automatically"; rollback
        if verify_service; then echo "rolled back, service restored. Stage 2 NOT applied."
        else echo "ROLLED BACK BUT SERVICE IS STILL NOT HEALTHY — ESCALATE"; fi
        exit 1
      fi ;;
  *) die "unknown mode: $1" ;;
esac
