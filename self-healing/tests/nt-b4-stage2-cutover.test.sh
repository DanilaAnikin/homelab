#!/usr/bin/env bash
# ============================================================================
# Rehearsal for the Stage 2 cutover, and for its rollback.
#
# Stage 2 is the change that can take login down for everyone. The matcher
# itself was measured against the real Traefik in nt-b4-stage2-matcher.test.sh;
# what this rehearses is the OTHER half — the file surgery, the pre-checks, and
# whether a failed post-check actually puts the old config back.
#
# Same shape as the Stage 1 rehearsal: the real script, `curl` stubbed on PATH,
# and the file manipulation deliberately NOT stubbed, because that is the part
# that can lose the site.
#
# The one thing this cannot rehearse is Traefik's reaction, which is why the
# matcher test exists separately and runs a real Traefik.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../nt-b4-stage2-cutover.sh"
OVERLAY="$HERE/../nt-b4-stage2-overlay.yml"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-stage2-cut.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

OK=0; BAD=0
note(){ printf '  %-6s %s\n' "$1" "$2"; }
pass(){ OK=$((OK+1)); note ok "$1"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1"; }

[[ -f "$SCRIPT" ]]  || { echo "missing $SCRIPT"; exit 1; }
[[ -f "$OVERLAY" ]] || { echo "missing $OVERLAY"; exit 1; }

STUB_STATE="$WORK/stub"; mkdir -p "$STUB_STATE"
STUB_BIN="$WORK/bin";   mkdir -p "$STUB_BIN"

# The live config AS STAGE 1 LEAVES IT — the bridge backend already in place.
STAGE1_YML='http:
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
        servers: [{ url: "http://natetrader-dashboard-bridge:3000" }]
        passHostHeader: true'

# The stub reads the LIVE CONFIG to decide whether the data plane is denied.
#
# A fixture that simply says "403" would pass the post-checks whether or not
# the overlay had been applied, so the rehearsal would be measuring the fixture.
# Modelling the one thing Traefik does that matters here — deny when the
# middleware is present, forward when it is not — is what makes the auto-
# rollback cases mean anything.
cat > "$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
# Every invocation's argv, verbatim, so the test can assert what did and did
# not appear on a command line. argv is world-readable in /proc; a secret that
# reaches it has leaked whether or not the request succeeds.
printf '%s\n' "$*" >> "$STUB_STATE/argv.log"
S="$STUB_STATE"
LIVE="$NT_TEST_LIVE"
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
key=other
case "$url" in
  */auth/v1/settings)  key=auth_settings ;;
  */auth/v1/token*)    key=auth_token ;;
  */auth/v1/user)      key=auth_user ;;
  */auth/v1/logout)    key=auth_logout ;;
  */api/health)        key=dash_health ;;
  */rest/v1/)          key=rest_root ;;
  *) key=deny ;;
esac
if [[ "$key" == rest_root || "$key" == deny ]]; then
  # a data-plane path: denied only when the overlay is actually live
  if contained; then code="$(cat "$S/code_deny" 2>/dev/null || echo 403)"
  else code="$(cat "$S/code_open" 2>/dev/null || echo 200)"; fi
else
  code="$(cat "$S/code_$key" 2>/dev/null || echo 200)"
fi
body="$(cat "$S/body_$key" 2>/dev/null || echo '{}')"
if [[ "$want_code" == 1 ]]; then printf '%s' "$code"; else printf '%s' "$body"; fi
exit 0
EOF
chmod +x "$STUB_BIN/curl"

healthy_world(){
  echo 200 > "$STUB_STATE/code_auth_settings"
  echo 200 > "$STUB_STATE/code_auth_token"
  echo '{"access_token":"stub-token"}' > "$STUB_STATE/body_auth_token"
  echo 200 > "$STUB_STATE/code_auth_user"
  echo 204 > "$STUB_STATE/code_auth_logout"
  echo 200 > "$STUB_STATE/code_dash_health"
  echo '{"artifact_role":"frozen-containment-bridge","writes_enabled":false}' > "$STUB_STATE/body_dash_health"
  # data-plane answers are decided by the stub from the live config, not here;
  # these are just the two values it chooses between
  echo 403 > "$STUB_STATE/code_deny"
  echo 200 > "$STUB_STATE/code_open"
}

fresh(){
  rm -rf "$WORK/dyn" "$WORK/backup" "$WORK/secrets"
  mkdir -p "$WORK/dyn" "$WORK/backup" "$WORK/secrets"
  printf '%s\n' "$STAGE1_YML" > "$WORK/dyn/natetrader.yml"
  chmod 600 "$WORK/dyn/natetrader.yml"
  : > "$STUB_STATE/argv.log"
  printf 'probe@example.invalid\nnot-a-real-password\n' > "$WORK/secrets/probe.txt"
  printf 'stub-anon-key\n' > "$WORK/secrets/anon.txt"
}

run_script(){
  PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" \
  DYN="$WORK/dyn" BACKUP_DIR="$WORK/backup" OVERLAY="$OVERLAY" \
  NT_TEST_LIVE="$WORK/dyn/natetrader.yml" \
  NT_PROBE_CRED="$WORK/secrets/probe.txt" NT_ANON_FILE="$WORK/secrets/anon.txt" \
  bash "$SCRIPT" "$1" >"$WORK/out.txt" 2>&1
}

echo "Stage 2 cutover rehearsal"
echo

# ── 1. happy path ───────────────────────────────────────────────────────────
fresh; healthy_world
if run_script --cutover; then pass "cutover succeeds in a healthy world"
else fail "cutover failed in a healthy world"; tail -4 "$WORK/out.txt"; fi

grep -q 'natetrader-deny-data-plane' "$WORK/dyn/natetrader.yml" \
  && pass "the deny middleware is present" || fail "the deny middleware is missing"
grep -q '!PathRegexp(`%`)' "$WORK/dyn/natetrader.yml" \
  && pass "the percent guard survived into the live file" \
  || fail "the percent guard is missing — the encoded-slash bypass would be open"

# The one that would silently undo Stage 1: the overlay ships the pre-Stage-1
# backend, so the script has to carry the live one across.
grep -q 'natetrader-dashboard-bridge:3000' "$WORK/dyn/natetrader.yml" \
  && pass "Stage 1's bridge backend was carried across" \
  || fail "Stage 2 REVERTED Stage 1 — traffic would go back to the unfrozen image"
grep -q 'http://natetrader-supabase-kong:8000' "$WORK/dyn/natetrader.yml" \
  && pass "the Kong service is intact" || fail "the Kong service was lost"
[[ "$(stat -c %a "$WORK/dyn/natetrader.yml")" == "600" ]] \
  && pass "file mode preserved (0600)" || fail "file mode changed"

# ── 2. rollback ─────────────────────────────────────────────────────────────
if run_script --rollback; then pass "rollback runs"; else fail "rollback failed"; fi
diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null \
  && pass "rollback restored the Stage 1 config BYTE FOR BYTE" \
  || fail "rollback did not restore the Stage 1 config"

# ── 3. a corrupt backup is refused ──────────────────────────────────────────
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true
echo corrupt >> "$(cat "$WORK/backup/stage2-rollback-target")"
if run_script --rollback; then fail "rollback restored a backup failing its own checksum"
else grep -q 'failed its own checksum' "$WORK/out.txt" \
       && pass "a corrupt backup is refused, for that reason" \
       || fail "refused, but not for the checksum reason"; fi

# ── 4. each pre-check can stop the cutover ──────────────────────────────────
blocks(){ # <label> <setup>
  fresh; healthy_world; eval "$2"
  # snapshot AFTER the setup: some setups deliberately alter the config, so
  # comparing against the pristine fixture would report every one of those as
  # "the script modified the file"
  local before; before="$(cat "$WORK/dyn/natetrader.yml")"
  if run_script --cutover; then fail "$1: proceeded anyway"
  elif [[ "$before" == "$(cat "$WORK/dyn/natetrader.yml")" ]]; then
    pass "$1: refused, file untouched"
  else fail "$1: refused BUT the file was modified"; fi
}
blocks "Stage 1 not applied"      'printf "%s\n" "${STAGE1_YML//natetrader-dashboard-bridge/natetrader-dashboard}" > "$WORK/dyn/natetrader.yml"'
blocks "Auth already down"        'echo 503 > "$STUB_STATE/code_auth_settings"'
blocks "dashboard already down"   'echo 502 > "$STUB_STATE/code_dash_health"'
blocks "data plane already 403"   'echo 403 > "$STUB_STATE/code_open"'
blocks "no probe identity"        'rm -f "$WORK/secrets/probe.txt"'
blocks "no anon key"              'rm -f "$WORK/secrets/anon.txt"'

# applying twice must not overwrite the rollback target with a contained config
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true
BEFORE="$(cat "$WORK/dyn/natetrader.yml")"
if run_script --cutover; then fail "re-apply: proceeded over itself"
elif [[ "$BEFORE" == "$(cat "$WORK/dyn/natetrader.yml")" ]]; then
  pass "re-apply: refused, file untouched"
else fail "re-apply: the file changed"; fi

# ── 5. a failing post-check rolls itself back ───────────────────────────────
# The dangerous case, and the specific one that matters: sign-in stops working.
fresh; healthy_world
echo 400 > "$STUB_STATE/code_auth_token"; echo '{}' > "$STUB_STATE/body_auth_token"
if run_script --cutover; then
  fail "sign-in broke and the script reported success"
elif diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
  pass "sign-in broke -> automatic rollback, Stage 1 config restored"
else
  fail "sign-in broke and the change was LEFT IN PLACE"
fi

# and the same for a data-plane path that is NOT denied after the change
fresh; healthy_world
echo 200 > "$STUB_STATE/code_deny"   # the overlay lands but Traefik does not deny
if run_script --cutover; then fail "the data plane stayed open and the script said success"
elif diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
  pass "data plane not denied -> automatic rollback"
else fail "data plane not denied and the change was left in place"; fi

# ── 6. non-vacuity ──────────────────────────────────────────────────────────
# Stage 2 REPLACES the file from the overlay rather than editing it in place,
# so Stage 1's marker-string probe does not transfer — a marker is SUPPOSED to
# disappear here. The equivalent question is whether the script is looking at
# the path the harness is driving at all.
fresh; healthy_world
rm -f "$WORK/dyn/natetrader.yml"
if run_script --cutover; then
  fail "non-vacuity: cutover succeeded with no config file — it is not reading \$DYN"
elif grep -q 'does not exist' "$WORK/out.txt"; then
  pass "non-vacuity: the script reads the path the harness drives"
else
  fail "non-vacuity: failed, but not because the file was missing"
fi

# and the converse: the overlay it reads is the real one on disk, not a copy
fresh; healthy_world
cp "$OVERLAY" "$WORK/overlay-broken.yml"
sed -i 's|natetrader-deny-data-plane|natetrader-deny-RENAMED|g' "$WORK/overlay-broken.yml"
if PATH="$STUB_BIN:$PATH" STUB_STATE="$STUB_STATE" DYN="$WORK/dyn" BACKUP_DIR="$WORK/backup" \
   OVERLAY="$WORK/overlay-broken.yml" NT_TEST_LIVE="$WORK/dyn/natetrader.yml" \
   NT_PROBE_CRED="$WORK/secrets/probe.txt" NT_ANON_FILE="$WORK/secrets/anon.txt" \
   bash "$SCRIPT" --cutover >"$WORK/out.txt" 2>&1; then
  fail "non-vacuity: an overlay with no deny middleware was applied"
else
  grep -q 'no deny middleware' "$WORK/out.txt" \
    && pass "non-vacuity: the overlay's content is actually inspected" \
    || fail "non-vacuity: refused, but not for the missing middleware"
fi

# ── C9: no secret may reach a command line ──────────────────────────────────
# The authenticated half of the matrix used to sign in with
#   curl -d '{"email":…,"password":…}'
# and then probe with
#   curl -H "Authorization: Bearer <token>"
# Both are visible in ps and /proc/<pid>/cmdline to any local user for the
# duration of the call, and in any process accounting or `bash -x` capture.
# They are now passed through a 0600 curl --config file and a request-body file
# on tmpfs, and the JSON is built through python's STDIN rather than its argv,
# because moving a password from curl's command line to python's is not a fix.
#
# This asserts the property directly, from the stub's own record of every argv
# it was handed — WITH a positive control, because "the password does not
# appear" is also what an empty log says.
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true
ARGV="$STUB_STATE/argv.log"
[[ -s "$ARGV" ]] && pass "C9 control: the argv log is non-empty ($(wc -l < "$ARGV") invocations)" \
                 || fail "C9 control: nothing was recorded — the assertions below would be vacuous"
grep -q 'stub-anon-key' "$ARGV" \
  && pass "C9 control: a value that IS on argv is found (the publishable anon key)" \
  || fail "C9 control: the log does not capture argv at all"
grep -q 'not-a-real-password' "$ARGV" \
  && fail "C9: the probe PASSWORD appeared on a curl command line" \
  || pass "C9: the probe password never reaches argv"
grep -q 'stub-token' "$ARGV" \
  && fail "C9: the bearer TOKEN appeared on a curl command line" \
  || pass "C9: the bearer token never reaches argv"
grep -q 'probe@example.invalid' "$ARGV" \
  && fail "C9: the probe identity appeared on a curl command line" \
  || pass "C9: the probe identity never reaches argv"

# ── C9b: the cleanup must actually remove the files ────────────────────────
# The first version tracked secret files in a bash array appended from inside
# `secret_tmp` — which is called as `hdrf="$(secret_tmp)"`, so the append ran in
# a COMMAND-SUBSTITUTION SUBSHELL and the parent's array stayed empty. The EXIT
# trap then removed nothing. Measured: 32 files, mode 0600, one of them holding
# a bearer token, left on /dev/shm after the rehearsals. The cleanup written to
# stop secrets persisting was itself a no-op, and nothing noticed because no
# test looked at the filesystem afterwards.
#
# Counting leftovers is the assertion; the control below proves the run created
# any in the first place, because "no files left" is also what a run that never
# made one produces.
# `ls` exits 2 when a glob matches nothing, and this file runs under
# `set -Eeuo pipefail`, so the obvious one-liner ABORTED the whole rehearsal
# (rc 2) instead of answering "zero". Counted with find, which reports an empty
# result as success.
leftovers(){
  { find /dev/shm -maxdepth 1 -name 'nt-b4-secret*' 2>/dev/null
    find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'nt-b4-secret*' 2>/dev/null
  } | grep -c . || true
}
before_leftovers="$(leftovers)"
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true
after_leftovers="$(leftovers)"
grep -q -- '--config' "$STUB_STATE/argv.log" \
  && pass "C9b control: the run really did create curl --config secret files" \
  || fail "C9b control: no --config was used, so 'nothing left behind' is vacuous"
[[ "$after_leftovers" -eq "$before_leftovers" ]] \
  && pass "C9b: the run left no secret file behind (${before_leftovers} before, ${after_leftovers} after)" \
  || fail "C9b: the run leaked $(( after_leftovers - before_leftovers )) secret file(s) onto tmpfs"

echo
echo "stage 2 rehearsal: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN — cutover and rollback both exercised"; exit 0; } \
                 || { echo "REHEARSAL RED"; exit 1; }
