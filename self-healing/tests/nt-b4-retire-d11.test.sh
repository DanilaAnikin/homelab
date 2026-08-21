#!/usr/bin/env bash
# ============================================================================
# Rehearsal for the retire step's PRE-CHECKS.
#
# nt-b4-deploy-bridge.test.sh exercises what retiring actually does — stop,
# clear the restart policy, detach, keep the container, restore from the
# record — against real containers on a real network. What it deliberately
# skipped is the half that decides whether any of that is allowed to happen,
# because those read the live Traefik config and the public hosts.
#
# That is the more dangerous half. Retiring the container that is currently
# serving is an outage you caused, and the only thing standing between the
# operator and that outage is a handful of `grep` and `curl` results. So each
# of them is broken in turn and required to stop the retire.
#
# Stubs on PATH; nothing real is stopped.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../nt-b4-retire-d11.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-retire.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

OK=0; BAD=0
note(){ printf '  %-6s %s\n' "$1" "$2"; }
pass(){ OK=$((OK+1)); note ok "$1"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1"; }

[[ -f "$SCRIPT" ]] || { echo "missing $SCRIPT"; exit 1; }

STUB_BIN="$WORK/bin"; STUB_STATE="$WORK/state"; mkdir -p "$STUB_BIN" "$STUB_STATE" "$WORK/dyn" "$WORK/lib"
LIVE="$WORK/dyn/natetrader.yml"

# The config as the END of B4 leaves it: bridge backend AND the deny middleware.
CONTAINED='http:
  routers:
    natetrader-api-auth:
      rule: "Host(`ntapi.anikin.cz`) && (Path(`/auth/v1`) || PathPrefix(`/auth/v1/`)) && !PathRegexp(`%`)"
      service: natetrader-kong
    natetrader-api:
      rule: "Host(`ntapi.anikin.cz`)"
      middlewares: [natetrader-deny-data-plane]
      service: natetrader-kong
    natetrader-dashboard:
      rule: "Host(`nate-trader.anikin.cz`)"
      service: natetrader-dashboard
  middlewares:
    natetrader-deny-data-plane:
      ipAllowList:
        sourceRange: ["255.255.255.255/32"]
  services:
    natetrader-kong:
      loadBalancer:
        servers: [{ url: "http://natetrader-supabase-kong:8000" }]
    natetrader-dashboard:
      loadBalancer:
        servers: [{ url: "http://natetrader-dashboard-bridge:3000" }]'

cat > "$STUB_BIN/docker" <<'EOF'
#!/usr/bin/env bash
# A docker stub rich enough to run retire() for real, and — crucially — able to
# FAIL the way the host fails. Every fail-open this rehearsal now covers was a
# check that produced its passing value when docker did not answer, so a stub
# that always answers cannot see any of them.
S="$STUB_STATE"
st(){ cat "$S/$1" 2>/dev/null; }
case "$1" in
  inspect)
    [[ "$(st old_state || echo running)" == absent ]] && exit 1
    case "$*" in
      *".State.Status"*)
        [[ -f "$S/inspect_state_fail" ]] && exit 1
        st old_state || echo running ;;
      *"NetworkSettings"*)
        # The failure that mattered: inspect exits non-zero having printed
        # nothing, and the old networks_of() turned that into an empty list.
        [[ -f "$S/inspect_nets_fail" ]] && exit 1
        st nets || echo "dokploy-network" ;;
      *"RestartPolicy"*)
        [[ -f "$S/inspect_policy_fail" ]] && exit 1
        st policy || echo "unless-stopped" ;;
      *) : ;;
    esac
    exit 0 ;;
  stop)
    [[ -f "$S/stop_fails" ]] && exit 1
    echo exited > "$S/old_state"; exit 0 ;;
  update)
    # A silent no-op update is the interesting case: rc 0 or rc 1, the policy
    # simply does not change.
    [[ -f "$S/update_noop" ]] || echo no > "$S/policy"
    [[ -f "$S/update_fails" ]] && exit 1
    exit 0 ;;
  network)
    case "$2" in
      disconnect)
        [[ -f "$S/disconnect_noop" ]] || : > "$S/nets"
        exit 0 ;;
    esac
    exit 0 ;;
  run)
    # `docker run --rm --network … busybox nslookup NAME`
    [[ -f "$S/docker_run_fails" ]] && exit 1     # e.g. busybox absent, offline
    local_name=""
    for a in "$@"; do local_name="$a"; done
    [[ -f "$S/resolves_$local_name" ]] && exit 0
    exit 1 ;;
esac
exit 0
EOF
cat > "$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
S="$STUB_STATE"
url=""; want=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w) [[ "$2" == *http_code* ]] && want=1; shift 2 ;;
    -o|--max-time|-H|-X|-d) shift 2 ;;
    -sS|-s|-S) shift ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */api/health)       key=dash ;;
  */auth/v1/verify)   key=auth ;;
  *) key=other ;;
esac
if [[ "$want" == 1 ]]; then cat "$S/code_$key" 2>/dev/null || echo 200
else cat "$S/body_$key" 2>/dev/null || echo '{}'; fi
EOF
chmod +x "$STUB_BIN/docker" "$STUB_BIN/curl"

healthy(){
  rm -f "$STUB_STATE"/inspect_nets_fail "$STUB_STATE"/inspect_policy_fail \
        "$STUB_STATE"/inspect_state_fail "$STUB_STATE"/update_fails \
        "$STUB_STATE"/update_noop "$STUB_STATE"/disconnect_noop \
        "$STUB_STATE"/docker_run_fails "$STUB_STATE"/stop_fails \
        "$STUB_STATE"/resolves_natetrader-dashboard
  echo "dokploy-network" > "$STUB_STATE/nets"
  echo "unless-stopped"  > "$STUB_STATE/policy"
  : > "$STUB_STATE/resolves_natetrader-dashboard-bridge"   # the prober's control
  echo running > "$STUB_STATE/old_state"
  echo 200 > "$STUB_STATE/code_dash"
  echo '{"artifact_role":"frozen-containment-bridge","writes_enabled":false}' > "$STUB_STATE/body_dash"
  echo 400 > "$STUB_STATE/code_auth"
  printf '%s\n' "$CONTAINED" > "$LIVE"
}

run(){ PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" STATE_DIR="$WORK/lib" DYN="$WORK/dyn" \
       NT_OLD_DASHBOARD=natetrader-dashboard NT_BRIDGE_CONTAINER=natetrader-dashboard-bridge \
       bash "$SCRIPT" --check >"$WORK/out.txt" 2>&1; }

echo "retire pre-check rehearsal"
echo

healthy
if run; then pass "pre-checks pass at the end of a completed B4"
else fail "pre-checks failed in the expected end state"; tail -6 "$WORK/out.txt"; fi

blocks(){ # <label> <setup> <expected fragment>
  healthy; eval "$2"
  if run; then fail "$1: allowed the retire"
  elif grep -qF "$3" "$WORK/out.txt"; then pass "$1: refused, and said why"
  else fail "$1: refused, but not for '$3'"; tail -3 "$WORK/out.txt"; fi
}

# The one that would cause the outage: retiring the container still serving.
blocks "Stage 1 not applied (traffic still on the old container)" \
  'sed -i "s|natetrader-dashboard-bridge:3000|natetrader-dashboard:3000|" "$LIVE"' \
  "Traefik does not point at"

# Retire is the step AFTER Stage 2, not a shortcut past it.
blocks "Stage 2 not applied" \
  'sed -i "/natetrader-deny-data-plane/d" "$LIVE"' \
  "Stage 2 is NOT in place"

blocks "the public dashboard is unhealthy" \
  'echo 502 > "$STUB_STATE/code_dash"' \
  "public dashboard"

blocks "what is serving is not the bridge" \
  'echo "{\"artifact_role\":\"dashboard\"}" > "$STUB_STATE/body_dash"' \
  "the bridge is not what is serving"

blocks "Auth is down" \
  'echo 503 > "$STUB_STATE/code_auth"' \
  "Auth"

blocks "there is no such container" \
  'echo absent > "$STUB_STATE/old_state"' \
  "no container named"

# Non-vacuity: the harness must be driving the file the script reads.
healthy
sed -i 's|natetrader-kong|natetrader-kong-RENAMED|g' "$LIVE"
run >/dev/null 2>&1 || true
grep -q 'natetrader-kong-RENAMED' "$LIVE" \
  && pass "non-vacuity: --check does not modify the config it reads" \
  || fail "non-vacuity: the config was modified by a read-only mode"

# ═══════════════════════════════════════════════════════════════════════════
# --retire ITSELF. Until now this was executed by no test in the repository:
# this file ran only --check, and the deploy rehearsal `sed`s networks_of() out
# and reimplements stop/update/disconnect inline, omitting every post-check —
# while its own header claims "the retire half IS fully covered". So the three
# assertions that decide whether containment actually happened had never run.
#
# Each of them had the same shape: its PASSING value was also its
# failure-to-run value.
# ═══════════════════════════════════════════════════════════════════════════
echo
echo "--retire, executed"
echo

run_retire(){ PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" STATE_DIR="$WORK/lib" DYN="$WORK/dyn" \
       NT_OLD_DASHBOARD=natetrader-dashboard NT_BRIDGE_CONTAINER=natetrader-dashboard-bridge \
       bash "$SCRIPT" --retire >"$WORK/out.txt" 2>&1; }

healthy
if run_retire; then pass "R-happy: a clean retire succeeds"
else fail "R-happy: the clean path failed"; tail -8 "$WORK/out.txt"; fi
grep -q 'detached from every network'   "$WORK/out.txt" && pass "R-happy: detachment asserted"       || fail "R-happy: no detachment assertion"
grep -q 'DNS prober control'            "$WORK/out.txt" && pass "R-happy: the prober control ran"    || fail "R-happy: no prober control"
grep -q 'restart policy cleared'        "$WORK/out.txt" && pass "R-happy: restart policy asserted"   || fail "R-happy: no restart-policy assertion"

r_blocks(){ # <label> <setup> <expected fragment>
  healthy; eval "$2"
  if run_retire; then fail "$1: reported success"
  elif grep -qF "$3" "$WORK/out.txt"; then pass "$1: refused, and said why"
  else fail "$1: failed, but not for '$3'"; tail -4 "$WORK/out.txt"; fi
}

# 1. docker cannot read the networks. The old code recorded an empty list,
#    called it "already detached", and later asserted "detached from every
#    network" from the same non-answer.
r_blocks "R1 networks unreadable is not 'already detached'" \
  'touch "$STUB_STATE/inspect_nets_fail"' \
  "refusing to retire a container whose"

# 2. the disconnect silently does nothing.
r_blocks "R2 a disconnect that does nothing is caught" \
  'touch "$STUB_STATE/disconnect_noop"' \
  "still attached to"

# 3. busybox is absent, so the resolver probe cannot run. The old code took the
#    else branch and asserted "no longer resolves".
r_blocks "R3 an unrunnable DNS probe is not a pass" \
  'touch "$STUB_STATE/docker_run_fails"' \
  "the DNS prober is not working"

# 4. the container is still resolvable — the real negative.
r_blocks "R4 a container that still resolves is caught" \
  'touch "$STUB_STATE/resolves_natetrader-dashboard"' \
  "still resolves on dokploy-network"

# 5. `docker update --restart=no` does not take. The old code announced success
#    unconditionally.
r_blocks "R5 a restart policy that did not clear is caught" \
  'touch "$STUB_STATE/update_noop"' \
  "restart policy NOT cleared"

# 6. the policy cannot be read at all.
r_blocks "R6 an unreadable restart policy is not 'nothing to clear'" \
  'touch "$STUB_STATE/inspect_policy_fail"' \
  "could not read the restart policy"

# 7. and the identity guard, which no state can satisfy.
healthy
if PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" STATE_DIR="$WORK/lib" DYN="$WORK/dyn" \
   NT_OLD_DASHBOARD=natetrader-dashboard-bridge NT_BRIDGE_CONTAINER=natetrader-dashboard-bridge \
   bash "$SCRIPT" --retire >"$WORK/out.txt" 2>&1; then
  fail "R7: retiring the BRIDGE ITSELF was allowed"
elif grep -q 'that is the BRIDGE' "$WORK/out.txt"; then
  pass "R7: refuses to retire the container serving public traffic"
else
  fail "R7: refused, but not for being the bridge"; tail -4 "$WORK/out.txt"
fi

# The two remaining stub knobs, exercised rather than left as dead machinery.
# `inspect_state_fail` and `update_fails` were wired into the stub and cleared
# by healthy() but set by no case, so they looked like coverage and were not.
r_blocks "R9 an unreadable state after stop is not 'stopped'" \
  'touch "$STUB_STATE/inspect_state_fail"' \
  "could not read"

r_blocks "R10 a docker update that errors is not a cleared policy" \
  'touch "$STUB_STATE/update_fails"; touch "$STUB_STATE/update_noop"' \
  "restart policy NOT cleared"

# 8. NON-VACUITY on this whole section. Every case above is "the run failed
#    with a particular message", and a script that could not start at all would
#    satisfy most of them. The happy path above is the control that it can
#    succeed; this asserts the mutations were actually reaching the code.
healthy
run_retire >/dev/null 2>&1 || true
grep -q 'the container still exists' "$WORK/out.txt" \
  && pass "R8 non-vacuity: the retire path really executed to its end" \
  || fail "R8 non-vacuity: the happy path never reached its final assertion"

# ── RET-3: the live-route guard must not pass by not looking ───────────────
# `[[ -r "$LIVE_CONFIG" ]] && grep …` short-circuited into the else branch, so
# "no live route points at $CONTAINER" was a positive assertion produced by
# never opening the file. And the regex required a double quote and a trailing
# colon, so two forms Traefik accepts — `url: 'http://x:3000'` and
# `url: http://x` — did not match.
healthy; chmod 000 "$LIVE"
if run; then fail "RET3a: an unreadable config was treated as clean"
elif grep -qF "cannot read" "$WORK/out.txt"; then pass "RET3a: an unreadable config is not 'no route points here'"
else fail "RET3a: refused, but not for unreadability"; tail -3 "$WORK/out.txt"; fi
chmod 644 "$LIVE"

# THE BRIDGE ROUTE STAYS. The first version of this case REPLACED the bridge
# URL, which removed the only occurrence of `natetrader-dashboard-bridge` from
# the fixture — so an earlier pre-check ("Traefik does not point at $BRIDGE")
# failed the run first, for every form, and the case passed without the guard
# under test ever being reached. It also had no message assertion at all, so it
# could not tell which check had refused. Both fixed: a SECOND service pointing
# at $CONTAINER is added alongside the bridge, and the message is required.
for form in "'http://natetrader-dashboard:3000'" "http://natetrader-dashboard" '"http://natetrader-dashboard:3000"'; do
  healthy
  python3 - "$LIVE" "$form" <<'PYX'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text().rstrip("\n")
s += ("\n    natetrader-legacy:\n"
      "      loadBalancer:\n"
      "        servers: [{ url: " + sys.argv[2] + " }]\n")
p.write_text(s)
PYX
  if run; then
    fail "RET3b: a live route in the form ${form} was not seen"
  elif grep -qF "still routes to natetrader-dashboard" "$WORK/out.txt"; then
    pass "RET3b: a live route written as ${form} is caught, and named"
  else
    fail "RET3b: refused for some other reason with ${form}"; tail -3 "$WORK/out.txt"
  fi
done
# NON-VACUITY: with no such route the same fixture must PASS, or the three cases
# above would be satisfied by any refusal at all.
healthy
if run; then pass "RET3b control: the unmodified fixture still passes"
else fail "RET3b control: the fixture fails even without a legacy route"; tail -3 "$WORK/out.txt"; fi

# ── RET-4: a failing docker call must report, not abort ────────────────────
# `docker stop` and the state inspect were bare under `set -e`, so a daemon
# hiccup killed the run after the container was stopped but before the restart
# policy was cleared and before the disconnect loop — no FAIL line, no summary,
# and the container left exactly in the state the disconnect exists to prevent.
healthy; touch "$STUB_STATE/stop_fails"
if run_retire; then fail "RET4: a failed docker stop was reported as success"
else pass "RET4: a failed docker stop is a reported failure"; fi
grep -qF "docker stop failed" "$WORK/out.txt" \
  && pass "RET4: and it says so" || { fail "RET4: no diagnosis"; tail -3 "$WORK/out.txt"; }
grep -qE 'retire: [0-9]+ ok' "$WORK/out.txt" \
  && pass "RET4: the run still reaches its summary instead of aborting mid-way" \
  || { fail "RET4: the run aborted before summarising"; tail -3 "$WORK/out.txt"; }
rm -f "$STUB_STATE/stop_fails"

echo
echo "retire pre-checks: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN"; exit 0; } || { echo "REHEARSAL RED"; exit 1; }
