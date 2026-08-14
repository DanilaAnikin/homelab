#!/usr/bin/env bash
# ============================================================================
# H-1 mutation suite — "two empty strings compare equal"
#
# Each scenario injects ONE realistic fault into the inputs of a real check
# site and asks the gate for a verdict. The gate MUST report FAIL.
#
#   NT_IMPL=legacy  reproduces the audited implementation verbatim.
#                   Scenarios are expected to be RED here: that is the proof
#                   the property was absent before the fix.
#   NT_IMPL=strict  uses lib/nt-verify.sh. Scenarios must be GREEN.
#
# Requires docker; brings up one throwaway postgres and destroys it.
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPL="${NT_IMPL:-strict}"
IMAGE="${NT_TEST_PG_IMAGE:-postgres:16-alpine}"
CN="ntv-h1-test-$$"
WORK="$(mktemp -d)"
TESTS=0; GOOD=0; BAD=0

cleanup(){ docker rm -f "$CN" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# shellcheck source=../lib/nt-verify.sh
. "$HERE/../lib/nt-verify.sh"

# ── the two implementations under test ──────────────────────────────────────
# Legacy: copied verbatim from nt-restore-drill.sh (check :40-43, mj :99, q :216)
LPASS=0; LFAIL=0
legacy_check(){ if [[ "$2" == "$3" ]]; then LPASS=$((LPASS+1)); else LFAIL=$((LFAIL+1)); fi; }
legacy_mj(){ python3 -c "import json,sys;d=json.load(open('$1'));print(eval(sys.argv[1],{'d':d}))" "$2" 2>/dev/null; }
legacy_q(){ docker exec "$CN" psql -U postgres -d postgres -Atq -c "$1" 2>/dev/null; }

# ── scenario harness ────────────────────────────────────────────────────────
# gate_verdict <manifest> <expr> <type> <sql> -> prints PASS or FAIL
gate_verdict(){
  local man="$1" expr="$2" ty="$3" sql="$4"
  if [[ "$IMPL" == legacy ]]; then
    LPASS=0; LFAIL=0
    legacy_check "scenario" "$(legacy_mj "$man" "$expr")" "$(legacy_q "$sql")"
    [[ $LFAIL -eq 0 ]] && echo PASS || echo FAIL
  else
    NTV_PASS=0; NTV_FAIL=0; NTV_FIRST_FAILURE=''
    ntv_define_psql "$CN" postgres postgres
    ntv_check "scenario" "$(ntv_json "$man" "$expr" "$ty")" "$(ntv_sql "$ty" "$sql")" >/dev/null
    [[ $NTV_FAIL -eq 0 ]] && echo PASS || echo FAIL
  fi
}

scenario(){ # scenario <name> <manifest> <expr> <type> <sql>
  local name="$1"; shift
  TESTS=$((TESTS+1))
  local got; got="$(gate_verdict "$@")"
  if [[ "$got" == FAIL ]]; then
    GOOD=$((GOOD+1)); printf '  ok    %-52s gate=FAIL\n' "$name"
  else
    BAD=$((BAD+1));  printf '  NOT-OK %-51s gate=PASS  <-- fault accepted\n' "$name"
  fi
}

# ── fixtures ────────────────────────────────────────────────────────────────
docker run -d --name "$CN" --network none -e POSTGRES_PASSWORD=x "$IMAGE" >/dev/null
for _ in $(seq 1 60); do docker exec "$CN" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
docker exec "$CN" pg_isready -U postgres -q || { echo "FATAL: test postgres never became ready"; exit 1; }

GOOD_MAN="$WORK/good.json"
cat > "$GOOD_MAN" <<'JSON'
{"snapshot_bound_counts":{"accounts":1},"source":{"extensions":"plpgsql=1.0"},
 "fingerprints":{"schema_sha256":"0000000000000000000000000000000000000000000000000000000000000000"},
 "typed_as_string":{"accounts":"1"}}
JSON
NOKEY_MAN="$WORK/nokey.json"; printf '{"snapshot_bound_counts":{}}' > "$NOKEY_MAN"
BADJSON_MAN="$WORK/bad.json";  printf '{"snapshot_bound_counts":{"accounts":1' > "$BADJSON_MAN"
TRUNC_MAN="$WORK/trunc.json";  head -c 40 "$GOOD_MAN" > "$TRUNC_MAN"
NULL_MAN="$WORK/null.json";    printf '{"source":{"extensions":null}}' > "$NULL_MAN"

echo "H-1 mutation suite  impl=$IMPL  image=$IMAGE"
echo

# ── the scenarios ───────────────────────────────────────────────────────────
# Each pairs a manifest-side fault with a database-side fault that a real
# restore failure would produce at the SAME check site. Under legacy both
# sides collapse to "" and the site scores PASS.

scenario "missing manifest key + SQL NULL" \
  "$NOKEY_MAN" "d['snapshot_bound_counts']['accounts']" int \
  "SELECT NULL::int"

scenario "malformed JSON manifest + SQL syntax error" \
  "$BADJSON_MAN" "d['snapshot_bound_counts']['accounts']" int \
  "SELECT count(*) FROM this_relation_does_not_exist"

scenario "truncated manifest + missing relation" \
  "$TRUNC_MAN" "d['snapshot_bound_counts']['accounts']" int \
  "SELECT count(*) FROM public.accounts"

scenario "manifest null + string_agg over empty set (the :234 case)" \
  "$NULL_MAN" "d['source']['extensions']" text \
  "SELECT string_agg(extname,',') FROM pg_extension WHERE extname='nonexistent'"

scenario "missing key + query returning no rows at all" \
  "$NOKEY_MAN" "d['snapshot_bound_counts']['accounts']" int \
  "SELECT 1 WHERE false"

# The dead-connection case points the runner at a container that does not
# exist, rather than killing the fixture the remaining scenarios still need.
_live_cn="$CN"; CN="${CN}-absent"
scenario "missing key + dead database connection" \
  "$NOKEY_MAN" "d['snapshot_bound_counts']['accounts']" int \
  "SELECT count(*) FROM pg_class"
CN="$_live_cn"

scenario "manifest records a count as a string, not an int" \
  "$GOOD_MAN" "d['typed_as_string']['accounts']" int \
  "SELECT 1"

echo
echo "H-1: $GOOD/$TESTS scenarios correctly rejected, $BAD accepted"
[[ $BAD -eq 0 ]] || { echo "H-1 SUITE RED (impl=$IMPL)"; exit 1; }
echo "H-1 SUITE GREEN (impl=$IMPL)"
