#!/usr/bin/env bash
# ============================================================================
# Does the B4 sequence COMPOSE?
#
# Stage 1, Stage 2 and the retire step each rehearse their own behaviour, and
# each asserts its own preconditions. None of that establishes that the
# preconditions of step N are actually produced by step N-1. Interfaces between
# correct steps are where sequences break: Stage 2 requires the live config to
# contain `natetrader-dashboard-bridge`, and it requires it because Stage 1
# writes it — but nothing until now ran them one after the other and checked.
#
# This drives the real scripts through the whole order against one shared
# config file:
#
#     stage1 --cutover  ->  stage2 --cutover  ->  (retire)  ->  unwind
#
# and then unwinds it in reverse, because a sequence you cannot walk backwards
# is not reversible no matter how reversible each step is on its own.
#
# `curl` is stubbed and answers from the live config, exactly as in the
# per-stage rehearsals. The file surgery is real.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S1="$HERE/../nt-b4-stage1-cutover.sh"
S2="$HERE/../nt-b4-stage2-cutover.sh"
OVERLAY="$HERE/../nt-b4-stage2-overlay.yml"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-seq.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

OK=0; BAD=0
note(){ printf '  %-6s %s\n' "$1" "$2"; }
pass(){ OK=$((OK+1)); note ok "$1"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1"; }

for f in "$S1" "$S2" "$OVERLAY"; do [[ -f "$f" ]] || { echo "missing $f"; exit 1; }; done

STUB_STATE="$WORK/stub"; STUB_BIN="$WORK/bin"; mkdir -p "$STUB_STATE" "$STUB_BIN" "$WORK/dyn" "$WORK/backup" "$WORK/secrets"
LIVE="$WORK/dyn/natetrader.yml"

PRISTINE='http:
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
printf '%s\n' "$PRISTINE" > "$LIVE"; chmod 600 "$LIVE"
printf 'probe@example.invalid\nnot-a-real-password\n' > "$WORK/secrets/probe.txt"
printf 'stub-anon-key\n' > "$WORK/secrets/anon.txt"

# ── stubs that answer from the live config ──────────────────────────────────
cat > "$STUB_BIN/docker" <<'EOF'
#!/usr/bin/env bash
S="$STUB_STATE"
case "$1" in
  inspect)
    case "$*" in
      *".State.Status"*)   echo running ;;
      *".RestartCount"*)   echo 0 ;;
      *"NetworkSettings"*) echo "dokploy-network " ;;
      *) exit 1 ;;
    esac; exit 0 ;;
  run)
    if printf '%s' "$*" | grep -q '/api/accounts'; then echo 401
    else echo '{"artifact_role":"frozen-containment-bridge","writes_enabled":false}'; fi
    exit 0 ;;
esac
exit 1
EOF

cat > "$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
LIVE="$NT_TEST_LIVE"
on_bridge(){ grep -q 'natetrader-dashboard-bridge' "$LIVE" 2>/dev/null; }
contained(){ grep -q 'natetrader-deny-data-plane' "$LIVE" 2>/dev/null; }
url=""; method=GET; want_code=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -w) [[ "$2" == *"http_code"* ]] && want_code=1; shift 2 ;;
    -o|-d|-H|--max-time) shift 2 ;;
    -sS|-s|-S|--path-as-is) shift ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
code=200; body='{}'
case "$url" in
  */auth/v1/settings) code=200 ;;
  */auth/v1/token*)   code=200; body='{"access_token":"stub"}' ;;
  */auth/v1/user)     code=200 ;;
  */auth/v1/logout)   code=204 ;;
  */login)            code=200 ;;
  */api/health)
      if on_bridge; then body='{"artifact_role":"frozen-containment-bridge","writes_enabled":false}'
      # MEASURED against the live host, not invented. The production dashboard's
  # /api/health carries no artifact_role at all; the stub used to answer
  # {"artifact_role":"dashboard","writes_enabled":true}, which no dashboard has
  # ever emitted, and that fiction is what let a rollback check that merely
  # counted 200s look adequate.
  else body='{"status":"ok","service":"nate-trader-dashboard","strategyVersion":"v11-adaptive-momentum","buildSha":"d11bbad8aad7ec98596b0d290cb938706982d069","dataMode":"account-scoped"}'; fi ;;
  */api/accounts)
      if [[ "$method" == GET ]]; then code=401
      elif on_bridge; then code=503; body='{"reason":"FROZEN_CONTAINMENT_BRIDGE"}'
      else code=401; body='{"code":"UNAUTHENTICATED"}'; fi ;;
  *)  # any other ntapi path is data plane
      if contained; then code=403; else code=200; fi ;;
esac
if [[ "$want_code" == 1 ]]; then printf '%s' "$code"; else printf '%s' "$body"; fi
exit 0
EOF
chmod +x "$STUB_BIN/docker" "$STUB_BIN/curl"

run1(){ PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" NT_TEST_LIVE="$LIVE" \
        DYN="$WORK/dyn" BACKUP_DIR="$WORK/backup" bash "$S1" "$1" >"$WORK/o1.txt" 2>&1; }
run2(){ PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" NT_TEST_LIVE="$LIVE" \
        DYN="$WORK/dyn" BACKUP_DIR="$WORK/backup" OVERLAY="$OVERLAY" \
        NT_PROBE_CRED="$WORK/secrets/probe.txt" NT_ANON_FILE="$WORK/secrets/anon.txt" \
        bash "$S2" "$1" >"$WORK/o2.txt" 2>&1; }

echo "B4 sequence rehearsal"
echo

# ── forward ─────────────────────────────────────────────────────────────────
# Stage 2 must refuse BEFORE Stage 1 — the order is a safety property, not a
# convention. Denying the data plane in front of an image that can still write
# is the wrong way round to fail.
if run2 --cutover; then fail "Stage 2 ran before Stage 1"
else grep -q 'Stage 1 is NOT in place' "$WORK/o2.txt" \
       && pass "Stage 2 refuses before Stage 1, and says why" \
       || fail "Stage 2 refused, but not because Stage 1 was missing"; fi
diff -q <(printf '%s\n' "$PRISTINE") "$LIVE" >/dev/null \
  && pass "the failed Stage 2 left the config untouched" || fail "Stage 2 modified the config"

run1 --cutover && pass "Stage 1 applies" || { fail "Stage 1 failed"; tail -4 "$WORK/o1.txt"; }

# THE INTERFACE: Stage 2's precondition must be satisfied by Stage 1's output.
# Not "both scripts mention the same string" — the actual file one wrote, read
# by the other.
run2 --check && pass "Stage 1's output satisfies Stage 2's pre-checks" \
             || { fail "Stage 1's output does NOT satisfy Stage 2"; tail -5 "$WORK/o2.txt"; }

run2 --cutover && pass "Stage 2 applies on top of Stage 1" \
               || { fail "Stage 2 failed after Stage 1"; tail -5 "$WORK/o2.txt"; }

grep -q 'natetrader-dashboard-bridge' "$LIVE" \
  && pass "Stage 1's route SURVIVED Stage 2" \
  || fail "Stage 2 reverted Stage 1 — traffic would return to the unfrozen image"
grep -q 'natetrader-deny-data-plane' "$LIVE" && pass "Stage 2's denial is present" || fail "Stage 2's denial is missing"
grep -q '!PathRegexp(`%`)' "$LIVE" && pass "the percent guard is present" || fail "the percent guard is missing"

# Both stages applied, and the whole point still holds from outside.
CODE(){ PATH="$STUB_BIN:$PATH" NT_TEST_LIVE="$LIVE" curl -sS -o /dev/null -w '%{http_code}' "$1" ${2:+-X "$2"}; }
[[ "$(CODE https://ntapi.anikin.cz/rest/v1/accounts)" == "403" ]] \
  && pass "end state: the data plane is denied" || fail "end state: the data plane is open"
[[ "$(CODE https://ntapi.anikin.cz/auth/v1/settings)" == "200" ]] \
  && pass "end state: Auth still works" || fail "end state: Auth is broken"
[[ "$(CODE https://nate-trader.anikin.cz/api/accounts POST)" == "503" ]] \
  && pass "end state: writes are frozen" || fail "end state: writes are not frozen"

# ── the WRONG order, which is the order the operator is most likely to try ──
# Stage 1 records its rollback target as the file from before Stage 1. Stage 2
# keeps a separate pointer and never updates Stage 1's. So `stage1 --rollback`
# run here — with both stages applied — restores a file with no auth-only
# router, no percent guard and no deny middleware, reopening the whole public
# data plane in one atomic rename.
#
# MEASURED, against this file's own fixture with the guard removed: the restore
# landed, `natetrader-deny-data-plane` was gone from the live config, the data
# plane answered 200, and the script printed ROLLBACK VERIFIED and exited 0 —
# because its three post-rollback probes are all satisfied with the data plane
# wide open. `nt-b4-retire-d11.sh` used to close by telling the operator to run
# exactly this command, and that instruction is only ever read in this state.
#
# This block exists because everything above unwinds in the CORRECT order, and
# a sequence test that only ever walks backwards correctly cannot see this.
live_before_wrong_order="$(cat "$LIVE")"
if run1 --rollback; then
  fail "Stage 1 rolled back WHILE STAGE 2 WAS LIVE and reported success"
  tail -4 "$WORK/o1.txt"
else
  pass "Stage 1 REFUSES to roll back while Stage 2 is live"
fi
grep -q 'natetrader-deny-data-plane' "$LIVE" \
  && pass "the refusal left the containment boundary in place" \
  || fail "the refused rollback still removed Stage 2's denial"
[[ "$(cat "$LIVE")" == "$live_before_wrong_order" ]] \
  && pass "the refused rollback changed nothing at all" \
  || { fail "the refused rollback modified the live config"; diff <(printf '%s' "$live_before_wrong_order") "$LIVE" | head -6; }
[[ "$(CODE https://ntapi.anikin.cz/rest/v1/accounts)" == "403" ]] \
  && pass "the data plane is STILL denied after the refused rollback" \
  || fail "the refused rollback reopened the data plane"

# ── backward ────────────────────────────────────────────────────────────────
# A sequence you cannot walk backwards is not reversible, however reversible
# each step is alone. Unwind in reverse order and require the pristine file.
run2 --rollback && pass "Stage 2 rolls back" || { fail "Stage 2 rollback failed"; tail -4 "$WORK/o2.txt"; }
grep -q 'natetrader-deny-data-plane' "$LIVE" && fail "Stage 2's denial survived its own rollback" \
                                             || pass "Stage 2's denial is gone"
grep -q 'natetrader-dashboard-bridge' "$LIVE" \
  && pass "Stage 1 is still in place after Stage 2 rolled back" \
  || fail "rolling back Stage 2 also undid Stage 1"

run1 --rollback && pass "Stage 1 rolls back" || { fail "Stage 1 rollback failed"; tail -4 "$WORK/o1.txt"; }
diff -q <(printf '%s\n' "$PRISTINE") "$LIVE" >/dev/null \
  && pass "the config is BYTE-IDENTICAL to where it started" \
  || { fail "the sequence did not fully unwind"; diff <(printf '%s\n' "$PRISTINE") "$LIVE" | head -6; }

[[ "$(CODE https://ntapi.anikin.cz/rest/v1/accounts)" == "200" ]] \
  && pass "after unwind: the data plane is reachable again" || fail "after unwind: still denied"
[[ "$(CODE https://nate-trader.anikin.cz/api/health)" != "" ]] && pass "after unwind: the dashboard answers" || fail "after unwind: no answer"

echo
echo "sequence: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "SEQUENCE GREEN — the steps compose, and unwind"; exit 0; } \
                 || { echo "SEQUENCE RED"; exit 1; }
