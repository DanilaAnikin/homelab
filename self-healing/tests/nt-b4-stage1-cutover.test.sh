#!/usr/bin/env bash
# ============================================================================
# Rehearsal for the Stage 1 cutover, and for its rollback.
#
# "There is a rollback script" is not a rollback path. A rollback path is one
# that has been run and observed to restore service. This runs the real script
# — not a copy, not a reimplementation — against a scratch directory with
# `docker` and `curl` stubbed on PATH, so every branch that can take the site
# down is exercised before any of it points at production.
#
# What is deliberately NOT stubbed: the file manipulation. The rename, the
# backup, the checksum, the diff guards and the restore all run for real
# against real files. That is the part that can lose the site, so that is the
# part the rehearsal must not simulate.
#
# The stubs are driven by files in $STUB_STATE, so a single test can make the
# bridge healthy, then unhealthy, and watch the script change its mind.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../nt-b4-stage1-cutover.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-stage1.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

OK=0; BAD=0
note(){ printf '  %-6s %s\n' "$1" "$2"; }
pass(){ OK=$((OK+1)); note ok "$1"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1"; }

[[ -f "$SCRIPT" ]] || { echo "missing $SCRIPT"; exit 1; }

STUB_STATE="$WORK/stub"; mkdir -p "$STUB_STATE"
STUB_BIN="$WORK/bin"; mkdir -p "$STUB_BIN"

LIVE_YML='http:
  routers:
    natetrader-api:
      rule: "Host(`ntapi.anikin.cz`)"
      service: natetrader-kong
      entryPoints: [web]
    natetrader-dashboard:
      rule: "Host(`nate-trader.anikin.cz`)"
      service: natetrader-dashboard
      entryPoints: [web]
  services:
    natetrader-kong:
      loadBalancer:
        servers: [{ url: "http://natetrader-supabase-kong:8000" }]
        passHostHeader: true
    natetrader-dashboard:
      loadBalancer:
        servers: [{ url: "http://natetrader-dashboard:3000" }]
        passHostHeader: true'

# ── stubs ───────────────────────────────────────────────────────────────────
cat > "$STUB_BIN/docker" <<'EOF'
#!/usr/bin/env bash
# state files decide what the world looks like this run
S="$STUB_STATE"
case "$1" in
  inspect)
    case "$*" in
      *".State.Status"*)   cat "$S/container_state" 2>/dev/null || echo absent ;;
      *".RestartCount"*)   cat "$S/restart_count" 2>/dev/null || echo 0 ;;
      *"NetworkSettings"*) cat "$S/networks" 2>/dev/null || echo "dokploy-network " ;;
      *) exit 1 ;;
    esac
    [[ "$(cat "$S/container_state" 2>/dev/null || echo absent)" == absent ]] && exit 1
    exit 0 ;;
  run)
    # `docker run ... curl` inside the pre-checks. Two different probes: the
    # health body, and the status of a protected read.
    if printf '%s' "$*" | grep -q '/api/accounts'; then
      cat "$S/internal_protected" 2>/dev/null || echo 401
    else
      cat "$S/internal_health" 2>/dev/null || true
    fi
    exit 0 ;;
esac
exit 1
EOF

# The health BODY is decided from the live config, not from a fixture.
#
# The first version of this stub returned the bridge's identity unconditionally,
# so it kept saying "frozen-containment-bridge" even after a rollback had
# pointed traffic back at the old dashboard. That masked a real bug: `--rollback`
# ran the post-cutover verification, which asserts the bridge's identity and its
# 503s — precisely what rolling back removes. A correct rollback would have
# reported failure in production. The Stage 2 rehearsal caught it because its
# stub read the config; this one now does too.
cat > "$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
# Answers by URL and method, from state files. -w '%{http_code}' means the
# caller wants the status; otherwise it wants the body.
S="$STUB_STATE"
LIVE="$NT_TEST_LIVE"
# The file says one thing; what Traefik is SERVING can lag it. $S/reload_lag,
# if present, is a countdown: that many calls still answer with the old backend
# even though the config has already changed. This is the only way to exercise
# the difference between waiting a fixed number of seconds and waiting for the
# change to be observable.
on_bridge(){
  grep -q 'natetrader-dashboard-bridge' "$LIVE" 2>/dev/null || return 1
  if [[ -f "$S/reload_lag" ]]; then
    local n; n="$(cat "$S/reload_lag")"
    if (( n > 0 )); then echo $(( n - 1 )) > "$S/reload_lag"; return 1; fi
  fi
  return 0
}
url=""; method=GET; want_code=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -w) [[ "$2" == *"http_code"* ]] && want_code=1; shift 2 ;;
    -o|-d|-H|--max-time) shift 2 ;;
    -sS|-s|-S) shift ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
key=""
case "$url" in
  */api/health)    key=health ;;
  */login)         key=login ;;
  */api/accounts)  key="accounts_$method" ;;
  */auth/v1/settings) key=auth ;;
  *) key=other ;;
esac
code="$(cat "$S/code_$key" 2>/dev/null || echo 200)"
body="$(cat "$S/body_$key" 2>/dev/null || echo '{}')"
# whoever the route points at is who answers
if [[ "$key" == health ]]; then
  if on_bridge; then body="$(cat "$S/body_health" 2>/dev/null || echo '{}')"
  # MEASURED against the live host, not invented. The production dashboard's
  # /api/health carries no artifact_role at all; the stub used to answer
  # {"artifact_role":"dashboard","writes_enabled":true}, which no dashboard has
  # ever emitted, and that fiction is what let a rollback check that merely
  # counted 200s look adequate.
  else body='{"status":"ok","service":"nate-trader-dashboard","strategyVersion":"v11-adaptive-momentum","buildSha":"d11bbad8aad7ec98596b0d290cb938706982d069","dataMode":"account-scoped"}'; fi
fi
if [[ "$key" == accounts_POST || "$key" == accounts_PUT || "$key" == accounts_PATCH || "$key" == accounts_DELETE ]]; then
  if ! on_bridge; then code=401; body='{"code":"UNAUTHENTICATED"}'; fi
fi
if [[ "$want_code" == 1 ]]; then printf '%s' "$code"; else printf '%s' "$body"; fi
exit 0
EOF
chmod +x "$STUB_BIN/docker" "$STUB_BIN/curl"

# a world in which everything is fine
healthy_world(){
  local bridge_body='{"artifact_role":"frozen-containment-bridge","writes_enabled":false}'
  echo running                 > "$STUB_STATE/container_state"
  echo 0                       > "$STUB_STATE/restart_count"
  echo "dokploy-network "      > "$STUB_STATE/networks"
  echo "$bridge_body"          > "$STUB_STATE/internal_health"
  echo 401                     > "$STUB_STATE/internal_protected"
  echo 200 > "$STUB_STATE/code_health";       echo "$bridge_body" > "$STUB_STATE/body_health"
  echo 200 > "$STUB_STATE/code_login"
  echo 200 > "$STUB_STATE/code_auth"
  echo 401 > "$STUB_STATE/code_accounts_GET"
  for v in POST PUT PATCH DELETE; do echo 503 > "$STUB_STATE/code_accounts_$v"; done
  echo '{"reason":"FROZEN_CONTAINMENT_BRIDGE"}' > "$STUB_STATE/body_accounts_POST"
}

fresh_dyn(){
  rm -rf "$WORK/dyn" "$WORK/backup"
  mkdir -p "$WORK/dyn" "$WORK/backup"
  printf '%s\n' "$LIVE_YML" > "$WORK/dyn/natetrader.yml"
  chmod 600 "$WORK/dyn/natetrader.yml"
}

run_script(){ # <mode>
  PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" \
  DYN="$WORK/dyn" BACKUP_DIR="$WORK/backup" NT_TEST_LIVE="$WORK/dyn/natetrader.yml" \
  bash "$SCRIPT" "$1" >"$WORK/out.txt" 2>&1
}

echo "Stage 1 cutover rehearsal"
echo

# ── 1. the happy path ───────────────────────────────────────────────────────
fresh_dyn; healthy_world
if run_script --cutover; then pass "cutover succeeds in a healthy world"
else fail "cutover failed in a healthy world"; sed -n '$p' "$WORK/out.txt"; fi

if grep -q 'natetrader-dashboard-bridge:3000' "$WORK/dyn/natetrader.yml"; then
  pass "the backend URL now points at the bridge"
else fail "the backend URL was not changed"; fi

# the guard that matters most: Stage 1 must not touch the API router
if grep -q 'http://natetrader-supabase-kong:8000' "$WORK/dyn/natetrader.yml"; then
  pass "the Kong service is untouched"
else fail "the Kong service was modified — that is Stage 2's job"; fi

CHANGED="$(diff <(printf '%s\n' "$LIVE_YML") "$WORK/dyn/natetrader.yml" | grep -cE '^[<>]' || true)"
if [[ "$CHANGED" == "2" ]]; then pass "exactly one line changed (2 diff lines)"
else fail "$CHANGED diff lines, expected 2"; fi

if [[ "$(stat -c %a "$WORK/dyn/natetrader.yml")" == "600" ]]; then
  pass "file mode preserved across the rename (0600)"
else fail "file mode changed to $(stat -c %a "$WORK/dyn/natetrader.yml")"; fi

# ── 2. rollback restores it byte for byte ───────────────────────────────────
if run_script --rollback; then pass "rollback runs"
else fail "rollback failed"; sed -n '$p' "$WORK/out.txt"; fi

if diff -q <(printf '%s\n' "$LIVE_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
  pass "rollback restored the file BYTE FOR BYTE"
else fail "rollback did not restore the original file"; fi

# ── 2b. rollback verifies SERVICE, not the change it just removed ───────────
# The bug this covers: `--rollback` used to run the post-cutover verification,
# which asserts the bridge's identity and its 503s — exactly what rolling back
# undoes. With a config-aware stub the old code fails here; the split into
# verify_service() is what makes it pass.
fresh_dyn; healthy_world
run_script --cutover >/dev/null 2>&1 || true
if run_script --rollback; then
  pass "rollback reports success once service is restored"
else
  fail "rollback restored the config but still reported failure"
  tail -4 "$WORK/out.txt"
fi

# ── 3. a rollback that cannot verify its own backup must refuse ─────────────
fresh_dyn; healthy_world
run_script --cutover >/dev/null 2>&1 || true
BK="$(cat "$WORK/backup/stage1-rollback-target")"
echo "corrupted" >> "$BK"
if run_script --rollback; then
  fail "rollback restored a backup that failed its own checksum"
else
  grep -q 'failed its own checksum' "$WORK/out.txt" \
    && pass "a corrupt backup is refused, with that reason" \
    || fail "rollback refused, but not for the checksum reason"
fi

# ── 4. every pre-check must be able to stop the cutover ─────────────────────
# A pre-check that cannot fail is decoration. Each of these breaks exactly one
# thing and requires that the file be left alone.
precheck_blocks(){ # <label> <setup-snippet>
  fresh_dyn; healthy_world; eval "$2"
  if run_script --cutover; then
    fail "$1: cutover proceeded anyway"
  elif diff -q <(printf '%s\n' "$LIVE_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
    pass "$1: refused, file untouched"
  else
    fail "$1: refused BUT the file was modified"
  fi
}

precheck_blocks "bridge container absent"      'echo absent > "$STUB_STATE/container_state"'
precheck_blocks "bridge restart-looping"       'echo 7 > "$STUB_STATE/restart_count"'
precheck_blocks "bridge off dokploy-network"   'echo "bridge " > "$STUB_STATE/networks"'
precheck_blocks "bridge health silent"         ': > "$STUB_STATE/internal_health"'
precheck_blocks "bridge is the WRONG image"    'echo "{\"artifact_role\":\"dashboard\"}" > "$STUB_STATE/internal_health"'
# The scenario that /api/health cannot see: the image serves, declares itself
# correctly, and answers 503 on every real page because SUPABASE_SERVER_URL is
# unset. The image currently in production predates that variable and its
# container does not set it, so a bridge started from production's environment
# would look healthy and be useless. Measured on a real container: GET
# /api/accounts is 503 without it and 401 with it.
precheck_blocks "bridge serves but is NOT configured" 'echo 503 > "$STUB_STATE/internal_protected"'
precheck_blocks "current site already broken"  'echo 502 > "$STUB_STATE/code_health"'
precheck_blocks "Auth already down"            'echo 503 > "$STUB_STATE/code_auth"'

# a file that is not the one this script was written for
fresh_dyn; healthy_world
sed -i 's|http://natetrader-dashboard:3000|http://something-else:3000|' "$WORK/dyn/natetrader.yml"
BEFORE="$(cat "$WORK/dyn/natetrader.yml")"
if run_script --cutover; then fail "unexpected current backend: cutover proceeded"
elif [[ "$BEFORE" == "$(cat "$WORK/dyn/natetrader.yml")" ]]; then
  pass "unexpected current backend: refused, file untouched"
else fail "unexpected current backend: file was modified"; fi

# ── 5. a failing POST-check must roll itself back ───────────────────────────
# The dangerous case: pre-checks pass, the rename lands, and the site is broken.
# The external checks only run after the rename, so breaking one of them is a
# faithful stand-in for "the route landed and the result is wrong".
fresh_dyn; healthy_world
echo '{"reason":"SOMETHING_ELSE"}' > "$STUB_STATE/body_accounts_POST"
if run_script --cutover; then
  fail "post-check failed but the script reported success"
else
  if diff -q <(printf '%s\n' "$LIVE_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
    pass "a failed post-check rolled itself back automatically"
  else
    fail "post-check failed and the change was LEFT IN PLACE"
  fi
fi

# ── 6. non-vacuity: the rehearsal can actually fail ─────────────────────────
# If the harness reports ok no matter what, everything above is decoration.
fresh_dyn; healthy_world
sed -i 's|natetrader-api|natetrader-api-RENAMED|' "$WORK/dyn/natetrader.yml"
run_script --cutover >/dev/null 2>&1 || true
if grep -q 'natetrader-api-RENAMED' "$WORK/dyn/natetrader.yml"; then
  pass "non-vacuity: the harness reads the real file it claims to read"
else fail "non-vacuity: the file the harness reads is not the one it edits"; fi

# ── C8: a reload slower than the wait must not trigger a rollback ───────────
# `sleep 5` was the only synchronization with Traefik's file watcher. Its
# default providersThrottleDuration is 2s and applies after the fsnotify
# debounce, so under load the routers can be live later than that. The
# verification then observed the OLD backend, scored a failure, and the cutover
# rolled itself back — a second live config flip caused by impatience alone,
# and a third if the operator retried. The wait now polls for the bridge to be
# answering instead of counting seconds.
#
# Here the stub answers as the OLD backend for the first six observations after
# the config has already changed. With a fixed short sleep that is a failed
# cutover; with the poll it is a slightly slower successful one.
fresh_dyn; healthy_world
echo 6 > "$STUB_STATE/reload_lag"
if NT_B4_WAIT_SECONDS=20 run_script --cutover; then
  pass "C8: a reload that lags the config change still cuts over cleanly"
else
  fail "C8: a lagging reload was treated as a failed cutover"; tail -6 "$WORK/out.txt"
fi
grep -q 'observed after' "$WORK/out.txt" \
  && pass "C8: the wait reports WHEN it observed the change, not how long it slept" \
  || fail "C8: no observation timing was reported — the poll may not have run"
grep -qi 'rolling back\|rolled back' "$WORK/out.txt" \
  && fail "C8: the lagging reload triggered a rollback" \
  || pass "C8: no rollback was triggered"

echo
echo "stage 1 rehearsal: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN — cutover and rollback both exercised"; exit 0; } \
                 || { echo "REHEARSAL RED"; exit 1; }
