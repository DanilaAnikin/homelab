#!/usr/bin/env bash
# ============================================================================
# Anti-regression: the REAL builder and the REAL drill must stay wired to the
# strict primitives.
#
# Every defect this programme fixed was a one-line revert away from coming
# back, and the fixes are invisible to any test that only exercises the
# libraries. The C-1 and C-4 suites prove the PRIMITIVES are correct; nothing
# in them notices if nt-recovery-set.sh quietly goes back to piping gpg into
# sha256sum. This file is the check that the production scripts actually call
# what we think they call.
#
# It is a static check on purpose. Running the real drill needs root, rclone,
# both component keys and a published recovery set; a guard that can only run
# in that environment is a guard that never runs.
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILDER="$HERE/../../scripts/nt-recovery-set.sh"
DRILL="$HERE/../nt-restore-drill.sh"
LIBV="$HERE/../lib/nt-verify.sh"
LIBC="$HERE/../lib/nt-crypto.sh"
CATSQL="$HERE/../lib/nt-catalogue.sql"

OK=0; BAD=0
ok(){   OK=$((OK+1));  printf '  ok     %s\n' "$1"; }
bad(){  BAD=$((BAD+1)); printf '  NOT-OK %-58s %s\n' "$1" "${2:-}"; }

# Match CODE, not prose. These scripts document the defects they replaced, so a
# naive grep for a legacy pattern hits the comment explaining why the pattern is
# gone — and a guard that fires on its own documentation gets deleted. `code()`
# strips full-line comments and keeps line numbers so a real hit is still
# locatable.
code(){ grep -nvE '^[[:space:]]*#' "$1"; }

# NB: never `code "$f" | grep -q …` here. Under `set -o pipefail`, `grep -q`
# exits the moment it matches, `code` upstream takes SIGPIPE and dies 141, and
# pipefail reports the whole pipeline as failed — so a pattern that matches
# EARLY is reported as ABSENT. That bit this very file: four true properties
# near the top of the drill flipped to NOT-OK the moment the file grew. Capture
# the output and test the string instead; the exit status of a pipeline whose
# reader quits early is not a fact about the file.
must_have(){ # <label> <file> <pattern>
  local hit; hit="$(code "$2" | grep -E "$3" | head -1 || true)"
  if [[ -n "$hit" ]]; then ok "$1"; else bad "$1" "pattern absent: $3"; fi; }
must_not_have(){ # <label> <file> <pattern>
  local hit; hit="$(code "$2" | grep -E "$3" | head -1 || true)"
  if [[ -n "$hit" ]]; then bad "$1" "LEGACY PATTERN PRESENT: $hit"; else ok "$1"; fi; }

echo "wiring: production scripts vs strict primitives"
echo

echo "-- both scripts exist and parse --"
for f in "$BUILDER" "$DRILL" "$LIBV" "$LIBC"; do
  [[ -r "$f" ]] || { bad "readable $(basename "$f")"; continue; }
  bash -n "$f" 2>/dev/null && ok "parses: $(basename "$f")" || bad "parses: $(basename "$f")"
done

echo
echo "-- C-1: no script may pipe gpg output into a consumer --"
# the exact legacy shape: `gpg ... -d ... | sha256sum`
must_not_have "builder does not pipe gpg -d into a digest" "$BUILDER" '\-d .*\|.*sha256sum'
must_not_have "drill does not pipe gpg -d into a digest"   "$DRILL"   '\-d .*\|.*sha256sum'
must_not_have "builder has no legacy verify_decrypts()"    "$BUILDER" '^verify_decrypts\(\)'
must_not_have "drill has no bare dec() helper"             "$DRILL"   '^dec\(\)'
must_have     "builder uses the authenticated round trip"  "$BUILDER" 'ntc_encrypt_and_verify'
must_have     "drill uses the quarantine primitive"        "$DRILL"   'ntc_decrypt_validated'

echo
echo "-- C-4: archive provenance --"
must_not_have "builder does not accept [[ -s ]] as completeness" "$BUILDER" '\[\[ -s "\$WORK/db\.dump" \]\]'
must_have     "builder round-trips the archive digest"           "$BUILDER" 'ntc_archive_roundtrip'
must_have     "builder reads the archive to the end"             "$BUILDER" 'ntc_archive_fully_consumable'
must_have     "drill reads the archive to the end"               "$DRILL"   'ntc_archive_fully_consumable'

echo
echo "-- H-1: no legacy string comparison or untyped accessor --"
must_not_have "drill has no legacy check()"  "$DRILL" '^check\(\)'
must_not_have "drill has no legacy mj()"     "$DRILL" '^mj\(\)'
must_not_have "drill has no legacy q()"      "$DRILL" '^q\(\)'
must_not_have "drill has no legacy psqlc()"  "$DRILL" '^psqlc\(\)'
must_have     "drill uses typed ntv_check"   "$DRILL" 'ntv_check'
must_have     "drill uses typed ntv_json"    "$DRILL" 'ntv_json'
must_have     "drill uses typed ntv_sql"     "$DRILL" 'ntv_sql'

echo
echo "-- H-3/H-4: one canonical catalogue, no hand-written fingerprint --"
must_not_have "builder has no inline relkind fingerprint" "$BUILDER" 'relkind::text'
must_not_have "drill has no inline relkind fingerprint"   "$DRILL"   'relkind::text'
must_not_have "builder has no unescaped pg_% exclusion"   "$BUILDER" "NOT LIKE 'pg_%'"
must_not_have "drill has no unescaped pg_% exclusion"     "$DRILL"   "NOT LIKE 'pg_%'"
must_have     "builder calls the shared catalogue"        "$BUILDER" 'ntv_catalogue'
must_have     "drill calls the shared catalogue"          "$DRILL"   'ntv_catalogue'
# the byte-identical requirement: both must name the SAME file, not a copy
must_have     "builder points at lib/nt-catalogue.sql"    "$BUILDER" 'nt-catalogue\.sql'
must_have     "drill points at lib/nt-catalogue.sql"      "$DRILL"   'nt-catalogue\.sql'

echo
echo "-- security domains are required properties, not INFO --"
must_not_have "drill prints no INFO-only rls line"        "$DRILL" 'INFO.*rls_policies'
must_not_have "drill prints no INFO-only acl line"        "$DRILL" 'INFO.*default_acls'

echo
echo "-- pwverifier is salted and excluded from the published digest --"
must_have     "catalogue salts the verifier"              "$CATSQL" ":'ntv_pw_salt'"
must_have     "catalogue refuses to run without a salt"   "$CATSQL" 'RAISE EXCEPTION'
must_have     "runner excludes pwverifier from the digest" "$LIBV"  "grep -v '\^pwverifier\|'"
must_have     "drill generates a fresh per-run salt"      "$DRILL"  'PW_SALT="\$\(openssl rand'
must_have     "builder generates a fresh per-run salt"    "$BUILDER" 'CAT_SALT="\$\(openssl rand'

echo
echo "-- H-7: strict mode, ERR inheritance, one verdict --"
must_have "builder sets -Eeuo pipefail"        "$BUILDER" 'set -Eeuo pipefail'
must_have "drill sets -Eeuo pipefail"          "$DRILL"   'set -Eeuo pipefail'
must_have "builder enables ERR inheritance"    "$BUILDER" 'inherit_errexit'
must_have "drill enables ERR inheritance"      "$DRILL"   'inherit_errexit'
must_have "drill installs the verdict traps"   "$DRILL"   'ntv_install_traps'
must_have "drill registers a cleanup hook"     "$DRILL"   'NTV_CLEANUP_HOOK'

echo
echo "-- C-2/C-3: negative controls assert a reason --"
must_have "drill's negatives take an expected class" "$DRILL" 'ntv_negative .* [a-z_]+ '
must_not_have "drill has no anything-nonzero negative()" "$DRILL" '^negative\(\)'
# the altered-root-key control must assert its prerequisites first
must_have "root-key control asserts readiness first"  "$DRILL" 'prerequisite'
must_not_have "root-key control does not score ERR as pass" "$DRILL" '\|\| echo ERR'
# the manifest-tamper control must invoke the real gate
must_have "manifest tamper invokes the real gate"     "$DRILL" 'real_manifest_gate'

echo
echo "-- no correctness error may be suppressed with || true --"
# `|| true` is legitimate only where a non-zero status carries no correctness
# meaning. Each exemption is a category, not a line number, so moving the code
# does not silently widen it:
#   grep -c        exits 1 on zero matches
#   rm/rmdir/umount/mountpoint/docker rm|volume|network   best-effort teardown
#   shopt/chmod/curl                                      advisory
#   rclone cat     exits 0 for a MISSING object, so its status is meaningless
#                  and the gate checks content instead — see marker_valid()
#   teardown       called once before provisioning, when nothing exists yet
BENIGN='grep -c|rm -f|rmdir|docker (rm|volume|network)|umount|mountpoint|shopt|chmod|curl|\$RC cat|teardown \|\| true|: > '
for f in "$BUILDER" "$DRILL"; do
  n="$(code "$f" | grep -cE '\|\| true' || true)"
  bad_lines="$(code "$f" | grep -E '\|\| true' | grep -vE "$BENIGN" || true)"
  if [[ -z "$bad_lines" ]]; then ok "no suppressed correctness errors in $(basename "$f") ($n benign)"
  else bad "no suppressed correctness errors in $(basename "$f")" "$(head -2 <<<"$bad_lines")"; fi
done

echo
echo "wiring: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "WIRING GREEN"; exit 0; } || { echo "WIRING RED"; exit 1; }
