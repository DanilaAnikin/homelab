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
S="$STUB_STATE"
case "$1" in
  inspect)
    # real docker prints NOTHING to stdout for a missing object and exits 1
    [[ "$(cat "$S/old_state" 2>/dev/null || echo running)" == absent ]] && exit 1
    case "$*" in
      *".State.Status"*) cat "$S/old_state" 2>/dev/null || echo running ;;
      *"NetworkSettings"*) echo "dokploy-network" ;;
      *) : ;;
    esac
    exit 0 ;;
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
  */auth/v1/settings) key=auth ;;
  *) key=other ;;
esac
if [[ "$want" == 1 ]]; then cat "$S/code_$key" 2>/dev/null || echo 200
else cat "$S/body_$key" 2>/dev/null || echo '{}'; fi
EOF
chmod +x "$STUB_BIN/docker" "$STUB_BIN/curl"

healthy(){
  echo running > "$STUB_STATE/old_state"
  echo 200 > "$STUB_STATE/code_dash"
  echo '{"artifact_role":"frozen-containment-bridge","writes_enabled":false}' > "$STUB_STATE/body_dash"
  echo 200 > "$STUB_STATE/code_auth"
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

echo
echo "retire pre-checks: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN"; exit 0; } || { echo "REHEARSAL RED"; exit 1; }
