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
url=""; method=GET; want_code=0; hdrs=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -w) [[ "$2" == *"http_code"* ]] && want_code=1; shift 2 ;;
    -H) hdrs="${hdrs}${2};"; shift 2 ;;   # RECORDED, not discarded — the
                                          # credential matrix varies these, so
                                          # the stub must be able to react to
                                          # them or that matrix cannot fail
    -o|-d|--max-time) shift 2 ;;
    -sS|-s|-S|--path-as-is) shift ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
key=other
case "$url" in
  # MEASURED: /auth/v1/verify is an open Kong route (400 from GoTrue);
  # /auth/v1/settings is behind key-auth (401 unauthenticated). The scripts
  # now probe the former, because the latter made every liveness assertion
  # here pass on a fiction and fail against the real host.
  */auth/v1/verify)    key=auth_settings ;;
  */auth/v1/token*)    key=auth_token
                       # $S/token_breaks_when_contained models the case that
                       # matters: sign-in WORKED before the change and stops
                       # working after it. Breaking it for the whole run instead
                       # makes the fixture indistinguishable from a credential
                       # that was already invalid — which is the real state of
                       # the live probe identity, and which must NOT be blamed
                       # on the cutover.
                       if [[ -f "$STUB_STATE/token_breaks_when_contained" ]] && contained; then
                         if [[ "$want_code" == 1 ]]; then printf '400'; else printf '{}'; fi
                         exit 0
                       fi ;;
  */auth/v1/user)      key=auth_user ;;
  */auth/v1/logout)    key=auth_logout ;;
  */api/health)        key=dash_health ;;
  */rest/v1/)          key=rest_root ;;
  *) key=deny ;;
esac
# MEASURED on the live host: some data-plane paths are ALREADY 403 before any
# containment exists — /rest/v1/ and /pg/ answer 403 from Kong's ACL plugin.
# $S/pre_denied lists paths that answer 403 regardless of the overlay, so the
# production shape can be reproduced here instead of assumed away.
if [[ -f "$STUB_STATE/pre_denied" ]]; then
  while IFS= read -r pd; do
    [[ -n "$pd" && "$url" == *"$pd" ]] && { [[ "$want_code" == 1 ]] && printf '403' || printf '{}'; exit 0; }
  done < "$STUB_STATE/pre_denied"
fi
if [[ "$key" == rest_root || "$key" == deny ]]; then
  # a data-plane path: denied only when the overlay is actually live
  if contained; then
    # $S/deny_depends_on_bearer models a denial that is NOT identity-independent:
    # a caller presenting `Authorization: Bearer` is let through while others are
    # denied. The credential matrix exists to catch exactly this, so the stub
    # must be able to PRODUCE it. Before -H was recorded the three credential
    # states were identical by construction and the matrix could never fail
    # (audit F3).
    if [[ -f "$STUB_STATE/deny_depends_on_bearer" && "$hdrs" == *"Authorization: Bearer "* ]]; then
      code="$(cat "$S/code_open" 2>/dev/null || echo 200)"
    else
      code="$(cat "$S/code_deny" 2>/dev/null || echo 403)"
    fi
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
  echo 400 > "$STUB_STATE/code_auth_settings"
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
: > "$STUB_STATE/token_breaks_when_contained"
if run_script --cutover; then
  fail "sign-in broke and the script reported success"
elif diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
  pass "sign-in broke -> automatic rollback, Stage 1 config restored"
else
  fail "sign-in broke and the change was LEFT IN PLACE"
fi
rm -f "$STUB_STATE/token_breaks_when_contained"

# THE CONVERSE, which is the live state: a credential that was ALREADY invalid
# must not be attributed to the cutover. Measured on the host — the probe
# identity returns invalid_credentials with no overlay in place — and a
# perfectly good containment change rolled itself back and escalated over it.
fresh; healthy_world
echo 400 > "$STUB_STATE/code_auth_token"; echo '{}' > "$STUB_STATE/body_auth_token"
if run_script --cutover; then
  pass "a pre-existing bad credential does not roll back working containment"
else
  fail "a sign-in that was already broken was blamed on the cutover"; tail -6 "$WORK/out.txt"
fi
grep -qF "authenticated half" "$WORK/out.txt" \
  && pass "and the run says the authenticated half is UNVERIFIED" \
  || { fail "the run did not disclose that the authenticated half is unverified"; tail -4 "$WORK/out.txt"; }

# and the same for a data-plane path that is NOT denied after the change
fresh; healthy_world
echo 200 > "$STUB_STATE/code_deny"   # the overlay lands but Traefik does not deny
if run_script --cutover; then fail "the data plane stayed open and the script said success"
elif diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null; then
  pass "data plane not denied -> automatic rollback"
else fail "data plane not denied and the change was left in place"; fi

# the credential matrix must be able to FAIL (audit F3). The stub used to
# discard -H, so no-auth/apikey/apikey+Bearer returned the identical status by
# construction and the "denial depends on the caller" branch was unreachable —
# the matrix asserted a property it could not have falsified. Model a denial
# that DOES depend on the caller (open to a Bearer, denied otherwise) and
# require the run to catch it and roll back.
fresh; healthy_world
: > "$STUB_STATE/deny_depends_on_bearer"
if run_script --cutover; then
  fail "a caller-dependent denial was NOT caught — the credential matrix is vacuous"
else
  grep -q 'denial depends on the caller' "$WORK/out.txt" \
    && pass "the credential matrix catches a denial that depends on the caller" \
    || { fail "cutover failed, but not because the denial depended on the caller"; tail -6 "$WORK/out.txt"; }
  diff -q <(printf '%s\n' "$STAGE1_YML") "$WORK/dyn/natetrader.yml" >/dev/null \
    && pass "a caller-dependent denial -> automatic rollback" \
    || fail "caller-dependent denial caught but the change was left in place"
fi
rm -f "$STUB_STATE/deny_depends_on_bearer"

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
# ATTRIBUTED TO THIS RUN. A global before/after count of everything matching
# nt-b4-secret* is affected by any concurrent run and by stale files from an
# earlier one, so it could pass or fail for reasons unrelated to the run under
# test. Listing the actual entries and diffing the SETS attributes each leftover
# to the run that created it.
leftovers_list(){
  { find /dev/shm -maxdepth 1 -name 'nt-b4-secret*' 2>/dev/null
    find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'nt-b4-secret*' 2>/dev/null
  } | LC_ALL=C sort
}
before_list="$(leftovers_list)"
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true
after_list="$(leftovers_list)"
new_entries="$(comm -13 <(printf '%s\n' "$before_list") <(printf '%s\n' "$after_list") | grep -c . || true)"
grep -q -- '--config' "$STUB_STATE/argv.log" \
  && pass "C9b control: the run really did create curl --config secret files" \
  || fail "C9b control: no --config was used, so 'nothing left behind' is vacuous"
[[ "$new_entries" -eq 0 ]] \
  && pass "C9b: the run left no secret file behind (no entry present after that was absent before)" \
  || { fail "C9b: the run leaked ${new_entries} secret file(s) onto tmpfs"
       comm -13 <(printf '%s\n' "$before_list") <(printf '%s\n' "$after_list") | head -3 | sed 's/^/           /'; }

# ── the production shape: SOME paths already denied, others open ───────────
# On the live host /rest/v1/ and /pg/ answer 403 from Kong's ACL with no
# containment deployed, while /rest/v1/accounts answers 200 with rows. The old
# pre-check pinned on /rest/v1/ alone and refused the whole cutover for a
# denial that had nothing to do with it; the old matrix then counted those two
# 403s as proof. Both are per-path now, and the run must still proceed.
fresh; healthy_world
printf '/rest/v1/\n/pg/\n' > "$STUB_STATE/pre_denied"
if run_script --cutover; then
  pass "P1: a partially pre-denied surface does not block the cutover"
else
  fail "P1: the cutover refused because two paths were already denied"; tail -5 "$WORK/out.txt"
fi
grep -qF "already denied before this change" "$WORK/out.txt" \
  && pass "P1: the pre-existing denials are named, not silently counted" \
  || { fail "P1: the pre-existing denials were not reported"; tail -3 "$WORK/out.txt"; }
grep -qE "the deny matrix is evidence.*6 of 8" "$WORK/out.txt" \
  && pass "P1: exactly the six that changed are counted as evidence" \
  || { fail "P1: the evidence count is wrong"; grep -i 'deny matrix' "$WORK/out.txt" | head -2; }

# And if EVERYTHING is already denied, the cutover must refuse: eight green
# lines would otherwise prove nothing at all.
fresh; healthy_world
printf '/\n/rest/v1/\n/rest/v1/accounts\n/storage/v1/object/public/x\n/realtime/v1/websocket\n/functions/v1/x\n/graphql/v1\n/pg/\n' > "$STUB_STATE/pre_denied"
if run_script --cutover; then
  fail "P2: a fully pre-denied surface was accepted"
else
  pass "P2: a fully pre-denied surface is refused — the change could not be attributed"
fi
rm -f "$STUB_STATE/pre_denied"

# ── B4-R4: credential-independence must not pass vacuously without a key ────
# With no anon key, all three "credential states" send empty headers and become
# identical, so "all three agree" is trivially true. That must be a FAIL, not a
# green — every other anon_key() consumer guards it; verify() was the gap.
fresh; healthy_world
run_script --cutover >/dev/null 2>&1 || true    # get to a contained live state
rm -f "$WORK/secrets/anon.txt"                  # now the key is unreadable
if run_script --verify; then
  fail "B4-R4: --verify passed with no anon key — credential-independence was vacuous"
else
  pass "B4-R4: --verify fails when the anon key is missing"
fi
grep -qF "credential-independence UNVERIFIED" "$WORK/out.txt" \
  && pass "B4-R4: and it names credential-independence as unverified" \
  || { fail "B4-R4: it failed, but not for the vacuous-key reason"; tail -4 "$WORK/out.txt"; }
fresh   # restore the key for any later cases

echo
echo "stage 2 rehearsal: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "REHEARSAL GREEN — cutover and rollback both exercised"; exit 0; } \
                 || { echo "REHEARSAL RED"; exit 1; }
