#!/usr/bin/env bash
# ============================================================================
# C-2 — a real negative control for the pgsodium ROOT KEY.
#
# Run as root (needs docker):   sudo self-healing/tests/c2-rootkey.test.sh
#
# WHAT THIS EXISTS TO REPLACE
# ---------------------------
# nt-restore-drill.sh's original "altered root key" control could pass for at
# least six reasons that had nothing whatever to do with the root key:
#
#   * no readiness assertion, so a container that never finished bootstrapping
#     scored identically to a rejected key;
#   * three `|| true`s, so a failed volume creation, a failed globals apply and
#     a failed restore were all indistinguishable from success;
#   * `2>/dev/null || echo ERR`, so the literal string "ERR" reached the
#     comparison, satisfied it, and was then printed as the *reason* inside the
#     control's own PASS message;
#   * consequently ANY unrelated restore, connection or container failure
#     satisfied the control.
#
# The property under test is the single most important one in the recovery set:
#
#     THE CAPTURED ROOT KEY, AND ONLY THE CAPTURED ROOT KEY, IS WHAT MAKES
#     vault.secrets DECRYPTABLE AFTER A RESTORE.
#
# A control may only say that if everything else that could break is proven NOT
# broken first, positively, by name, in the same run. So each scenario asserts a
# full prerequisite chain BEFORE it is allowed to score anything:
#
#   P1  the exact image supabase/postgres:17.6.1.136, pinned by image ID, with
#       every container in the run verified to be running that same ID
#   P2  SEMANTIC readiness: 5 CONSECUTIVE successful queries as supabase_admin.
#       NOT pg_isready — this image restarts postgres partway through bootstrap
#       and pg_isready reports "accepting connections" during a window in which
#       every query still fails with "the database system is starting up"
#   P3  globals reconciled, and provably load-bearing: the fixture's canary
#       table is owned by a role a fresh image does NOT have, so the restore
#       cannot succeed at all unless globals were really applied
#   P4  a FRESH target database, asserted empty before the restore
#   P5  strict `pg_restore --exit-on-error` exit 0, and zero error lines
#   P6  the exact expected vault.secrets CIPHERTEXT row count, every row
#       carrying non-empty ciphertext
#   P7  unrelated catalogue invariants intact: canary row count, canary content
#       digest, canary owner and the extension set — none of which have
#       anything to do with the root key
#
# Only when P1..P7 all pass is the root key varied and the outcome scored. If
# any prerequisite fails, the scenario is reported NOT SCORED and counted as a
# failure: "something else broke" must never look like "the control worked".
#
# WHAT WAS MEASURED  (2026-08-15, supabase/postgres:17.6.1.136,
# image sha256:f519727303f0…; every expectation below is an OBSERVED result)
# ---------------------------------------------------------------------------
# The mechanism. This image's /etc/postgresql/postgresql.conf sets
#     pgsodium.getkey_script = vault.getkey_script
#                            = /usr/lib/postgresql/bin/pgsodium_getkey.sh
# and that script is, in full:
#     KEY_FILE=/etc/postgresql-custom/pgsodium_root.key
#     if [[ ! -f "${KEY_FILE}" ]]; then
#         head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n' > "${KEY_FILE}"
#     fi
#     cat $KEY_FILE
# supabase_vault 0.3.1's vault.decrypted_secrets decrypts with
#     vault._crypto_aead_det_decrypt(..., key_id => 0::bigint, context => 'pgsodium')
# i.e. directly under the server root key. This version has no pgsodium.key
# table, so that one file is the entire secret material.
#
#   1  correct key, installed pre-boot   ready 8s, restore 0, DECRYPTS, and the
#                                        plaintext digests match the fixture
#   2  key absent                        boots FINE. pgsodium_getkey.sh SILENTLY
#                                        GENERATES a fresh random key (mode
#                                        0640) and the cluster comes up healthy.
#                                        Decryption fails, SQLSTATE 22000,
#                                        "pgsodium_crypto_aead_det_decrypt_by_id:
#                                         invalid ciphertext"
#   3  valid-format wrong 64-hex key     identical to (2): SQLSTATE 22000
#   4  wrong owner (root:root, 0600)     the POSTMASTER REFUSES TO START:
#                                        "cat: can't open '…/pgsodium_root.key':
#                                         Permission denied" then
#                                        "FATAL:  invalid secret key", container
#                                        exits 1. A different failure class from
#                                        (2)/(3) — and exactly the kind of
#                                        unrelated blow-up the old control
#                                        counted as a pass.
#   5a wrong mode 0644, owner postgres   *** SECRETS STILL DECRYPT ***
#   5b wrong mode 0000, owner postgres   same class as (4): refuses to start
#   6  key under the wrong filename      boots FINE, the canonical path is
#                                        SILENTLY REGENERATED, the operator's
#                                        real key sits unused beside it,
#                                        SQLSTATE 22000
#   7  correct key installed only AFTER
#      first boot                        22000 before; STILL 22000 after the
#                                        correct key is written with no restart;
#                                        DECRYPTS only once the postmaster is
#                                        restarted
#
# THREE FINDINGS THIS SUITE ASSERTS RATHER THAN HIDES
# ---------------------------------------------------
# F1  File MODE is not a control. 0644 decrypts perfectly (5a). The functional
#     boundary is "readable by the postgres user", not the mode value: 0000 and
#     root-owned 0600 both fail, and they fail by refusing to start rather than
#     by failing to decrypt. A drill that only asks "did the secrets decrypt"
#     cannot tell a mode regression from a correct install, and the drill's own
#     `chmod 600` is hygiene, not an enforced property.
# F2  A MISSING key is silent. Absent (2) and misnamed (6) both yield a
#     healthy-looking cluster with a freshly minted random root key, and that
#     key is PERSISTED into the config volume. Nothing in the boot path
#     complains. Only a decryption test finds it — which is exactly why this
#     control has to actually work.
# F3  The root key is read ONCE, at postmaster start (7). Installing it later
#     changes nothing until a restart. "Installed before first start" is a real
#     requirement, but the precise property is "before the postmaster that will
#     serve the decryption starts", and a restart repairs it.
#
# For (4) and (5b), where the failure is a refusal to boot, the scenario also
# runs a REPAIR CONTROL: it fixes ONLY the varied property, on the very same
# config volume, and requires the whole happy path to succeed. Without that,
# "the container did not start" is attributable to anything at all — which is
# the original defect wearing a different hat.
#
# SECRET HANDLING
# ---------------
# The root key and the fixture plaintexts are generated on the host into
# mode-0600 files inside a mode-0700 directory, never appear in any argv (only
# in shell builtins, which create no process-list entry), are never printed, and
# are shredded on exit. Only 16-hex-character digest prefixes are displayed.
#
# SCANS
# -----
# Every textual scan (boot logs, TOC, globals) is literal-only, never piped, and
# is preceded by a two-sided self-test on the very file being scanned: a token
# that MUST be present must read `yes`, and a per-run impossible token must read
# `no`. An "absent" verdict from a scanner that has not passed that test is not
# trusted.
#
# ENV
#   NT_C2_IMAGE     override the image (default: the exact pinned tag)
#   NT_C2_ONLY      comma-separated scenario slugs; prints PARTIAL RUN
#   NT_C2_SABOTAGE  falsifiability: name one scenario slug and its key is
#                   installed CORRECTLY instead of varied. That scenario MUST
#                   then go red; if the suite stays green the suite is worthless
#                   and this exits non-zero.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

IMAGE="${NT_C2_IMAGE:-supabase/postgres:17.6.1.136}"
ONLY="${NT_C2_ONLY:-}"
SABOTAGE="${NT_C2_SABOTAGE:-}"
READY_STREAK=5
READY_BUDGET=180

RUN="c2-$$-$RANDOM"
W="$(mktemp -d "${TMPDIR:-/tmp}/c2test.XXXXXX")"; chmod 700 "$W"
ABSENT_TOKEN="impossible-token-$(openssl rand -hex 12)"

VAULT_N=2
CANARY_N=500
FIX_ROLE=nt_c2_owner
TARGET_DB=nt_c2_restore

OK=0; BAD=0; FIRST=''
LEAKS=0
VERDICT_DONE=0
SABOTAGE_HIT=0
READY_S=0; READY_STATE=''
declare -a CONTAINERS=() VOLUMES=()

# ── output and assertions ───────────────────────────────────────────────────
note(){ printf '  %-7s %-62s %s\n' "$1" "$2" "${3:-}"; }
pass(){ OK=$((OK+1)); note PASS "$1" "${2:-}"; }
fail(){ BAD=$((BAD+1)); [[ -n "$FIRST" ]] || FIRST="$1 :: ${2:-}"; note FAIL "$1" "${2:-}"; }
info(){ note INFO "$1" "${2:-}"; }
finding(){ printf '  %-7s %s\n' '***' "FINDING: $1"; }
hdr(){ printf '\n=== %s\n' "$*"; }

# Two absences must never compare equal (the H-1 defect that scored `check "" ""`
# as a PASS eleven times). An empty producer is a failure OF THE PRODUCER, never
# evidence about the property.
assert_eq(){ # <name> <expected> <actual>
  local n="$1" e="$2" a="$3"
  if [[ -z "$e" ]]; then fail "$n" "expected side is empty — the assertion would be vacuous"; return 0; fi
  if [[ -z "$a" ]]; then fail "$n" "actual side is empty — the producer did not execute"; return 0; fi
  if [[ "$e" == "$a" ]]; then pass "$n" "$a"; else fail "$n" "expected=$e actual=$a"; fi
}
assert_ne(){ # <name> <must-differ-from> <actual> [why]
  local n="$1" e="$2" a="$3"
  if [[ -z "$e" || -z "$a" ]]; then fail "$n" "one side is empty — cannot assert a difference"; return 0; fi
  if [[ "$e" != "$a" ]]; then pass "$n" "${4:-differs}"; else fail "$n" "values are identical ($a)"; fi
}

# ── teardown, leak check, single verdict ────────────────────────────────────
cleanup(){
  local c v rc
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do
    rc=0; docker rm -f "$c" >/dev/null 2>&1 || rc=$?
  done
  for v in ${VOLUMES[@]+"${VOLUMES[@]}"}; do
    rc=0; docker volume rm -f "$v" >/dev/null 2>&1 || rc=$?
  done
  # The teardown statuses above are best-effort (a container may already be
  # gone). The real assertion is that nothing belonging to this run survives.
  local left_c left_v
  left_c="$(docker ps -a --filter "name=ntc2-$RUN" --format '{{.Names}}' 2>/dev/null | wc -l)"
  left_v="$(docker volume ls --filter "name=ntc2v-$RUN" --format '{{.Name}}' 2>/dev/null | wc -l)"
  LEAKS=$(( left_c + left_v ))
  if [[ -d "$W" ]]; then
    rc=0; find "$W" -type f -exec shred -u {} + >/dev/null 2>&1 || rc=$?
    rm -rf "$W"
  fi
  return 0
}

verdict(){ # <shell exit status>
  [[ $VERDICT_DONE -eq 0 ]] || return 0
  VERDICT_DONE=1
  local rc="$1"
  if [[ "${LEAKS:-0}" -gt 0 ]]; then
    BAD=$((BAD+1))
    [[ -n "$FIRST" ]] || FIRST="teardown left $LEAKS resource(s) behind"
    printf '  %-7s %s\n' FAIL "teardown leaked $LEAKS container(s)/volume(s) named for run $RUN"
  fi
  printf '\nC-2: %d ok, %d not-ok   (first failure: %s)\n' "$OK" "$BAD" "${FIRST:-none}"
  [[ -z "$ONLY" ]] || printf 'PARTIAL RUN (NT_C2_ONLY=%s) — this is NOT a full suite result\n' "$ONLY"

  if [[ -n "$SABOTAGE" ]]; then
    printf '\nFALSIFIABILITY RUN: scenario %q had the CORRECT key installed on purpose.\n' "$SABOTAGE"
    if [[ $BAD -gt 0 && $SABOTAGE_HIT -gt 0 ]]; then
      printf 'C-2 FALSIFIABILITY CONFIRMED — the sabotaged scenario went red (%d failure(s) inside it).\n' "$SABOTAGE_HIT"
      exit 0
    fi
    printf 'C-2 FALSIFIABILITY CHECK FAILED — the suite did not detect a deliberately correct key.\n'
    printf '   (total not-ok=%d, failures inside the sabotaged scenario=%d)\n' "$BAD" "$SABOTAGE_HIT"
    exit 1
  fi

  if [[ $rc -ne 0 && $BAD -eq 0 ]]; then
    printf 'C-2 SUITE RED — aborted with status %d without scoring a failure.\n' "$rc"; exit 1
  fi
  if [[ $BAD -eq 0 && $OK -gt 0 ]]; then echo "C-2 SUITE GREEN"; exit 0; fi
  echo "C-2 SUITE RED"; exit 1
}

on_err(){
  local rc=$?
  BAD=$((BAD+1))
  [[ -n "$FIRST" ]] || FIRST="uncaught command failure (status $rc) near line ${BASH_LINENO[0]:-?}"
  printf '  %-7s uncaught command failure (status %s) near line %s\n' FAIL "$rc" "${BASH_LINENO[0]:-?}"
}
on_exit(){ local rc=$?; cleanup; verdict "$rc"; }
trap on_err ERR
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── literal-only scanning ───────────────────────────────────────────────────
# `grep -q` as the reader of a pipeline under pipefail INVERTS the verdict (the
# early exit SIGPIPEs the writer), and a discarded status cannot distinguish
# "no match" from "could not read the file". Nothing here is piped, and exit 1
# (absent) is kept distinct from exit >=2 (broken scan).
log_has(){ # <file> <literal> -> prints yes|no; returns 1 only if the scan broke
  local rc=0
  grep -qF -- "$2" "$1" || rc=$?
  case $rc in
    0) printf yes; return 0 ;;
    1) printf no;  return 0 ;;
    *) printf SCAN_BROKEN; return 1 ;;
  esac
}
count_lines_with(){ # <file> <literal> -> prints a count, or SCAN_BROKEN
  local n rc=0
  n="$(grep -cF -- "$2" "$1")" || rc=$?
  case $rc in
    0) printf '%s' "$n"; return 0 ;;
    1) printf 0; return 0 ;;
    *) printf SCAN_BROKEN; return 1 ;;
  esac
}
# An "absent" verdict is only worth anything if the scanner has been shown, on
# the very file being scanned, to report both present and absent correctly.
scanner_ok(){ # <label> <file> <literal-that-must-be-present>
  local p a prc=0 arc=0
  p="$(log_has "$2" "$3")" || prc=$?
  a="$(log_has "$2" "$ABSENT_TOKEN")" || arc=$?
  if [[ $prc -ne 0 || $arc -ne 0 || "$p" != yes || "$a" != no ]]; then
    fail "$1 scanner self-test" "present='$p'(rc=$prc) impossible='$a'(rc=$arc) — verdicts on this file are not trustworthy"
    return 1
  fi
  pass "$1 scanner self-test" "control present=yes, impossible token=no"
  return 0
}

# ── docker / sql helpers ────────────────────────────────────────────────────
# Every accessor returns a NON-EMPTY value in every outcome, so a broken
# producer can never be mistaken for a legitimate empty result.
dsql(){ # <cn> <db> <sql> -> value | ERR:<class>
  local out rc=0
  out="$(docker exec "$1" psql -U supabase_admin -d "$2" -X -A -t -q -v ON_ERROR_STOP=1 -c "$3" 2>"$W/sql.err")" || rc=$?
  if [[ $rc -ne 0 ]]; then printf 'ERR:sql_exec'; return 0; fi
  if [[ -z "$out" ]]; then printf 'ERR:no_rows'; return 0; fi
  printf '%s' "$out"
}
dsql_rc(){ # <cn> <db> <sql> -> psql's exit status
  local rc=0
  docker exec "$1" psql -U supabase_admin -d "$2" -X -A -t -q -v ON_ERROR_STOP=1 -c "$3" >/dev/null 2>"$W/sql.err" || rc=$?
  printf '%s' "$rc"
}
dsql_join(){ local v; v="$(dsql "$1" "$2" "$3")"; printf '%s' "${v//$'\n'/ }"; }

img_of(){      docker inspect -f '{{.Image}}'          "$1" 2>/dev/null || printf 'ERR:inspect'; }
state_of(){    docker inspect -f '{{.State.Status}}'   "$1" 2>/dev/null || printf 'ERR:inspect'; }
exitcode_of(){ docker inspect -f '{{.State.ExitCode}}' "$1" 2>/dev/null || printf 'ERR:inspect'; }

# 5 CONSECUTIVE successful queries AS THE ROLE WE WILL ACTUALLY USE.
# 0 = ready, 1 = the container left 'running', 2 = budget exhausted.
wait_ready(){ # <cn>
  local cn="$1" streak=0 i st
  for ((i=0; i<READY_BUDGET; i++)); do
    st="$(state_of "$cn")"
    if [[ "$st" != running ]]; then READY_S="$i"; READY_STATE="$st"; return 1; fi
    if docker exec "$cn" psql -U supabase_admin -d postgres -X -Atqc 'SELECT 1' >/dev/null 2>&1; then
      streak=$((streak+1))
      if [[ $streak -ge $READY_STREAK ]]; then READY_S="$i"; READY_STATE=running; return 0; fi
    else
      streak=0
    fi
    sleep 1
  done
  READY_S="$READY_BUDGET"; READY_STATE=timeout; return 2
}

# Reads a single-line file into a SQL literal using builtins only: the value
# never becomes an argument of any external process, so it cannot appear in the
# host process list.
#
# `read` returns 1 when the final line has no trailing newline — which is
# exactly how these fixture files are written (openssl | tr -d '\n'). The value
# IS still assigned, so that status must be tolerated deliberately rather than
# be allowed to kill the command substitution under `set -e`/inherit_errexit.
# It cannot be tolerated silently either: an unreadable file also lands here,
# and an empty literal would splice into syntactically valid-looking SQL. So an
# empty read emits a bare token that is guaranteed to raise a SQL error.
sql_lit(){
  local v=''
  if ! IFS= read -r v < "$1"; then :; fi
  if [[ -z "$v" ]]; then printf 'SQL_LIT_READ_FAILED'; return 0; fi
  v="${v//\'/\'\'}"
  printf "'%s'" "$v"
}

dump_logs(){ # <cn> <outfile>
  local rc=0
  docker logs "$1" > "$2" 2>&1 || rc=$?
  if [[ $rc -ne 0 ]]; then printf '(docker logs failed with status %s)\n' "$rc" >> "$2"; fi
  return 0
}

# ── preflight ───────────────────────────────────────────────────────────────
hdr "preflight"
docker info >/dev/null 2>&1 || { echo "FATAL: no usable docker daemon (run this as root / under sudo)"; exit 1; }

IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || printf '')"
if [[ -z "$IMAGE_ID" ]]; then
  echo "FATAL: image $IMAGE is not present locally."
  echo "       This suite deliberately does not pull. The property under test is bound to one"
  echo "       exact image, and a pull is precisely how a drifting tag gets in."
  exit 1
fi
IMAGE_TAGS="$(docker image inspect "$IMAGE" --format '{{join .RepoTags ","}}')"
assert_eq "image tag is the exact pinned ref" "yes" \
  "$(case ",$IMAGE_TAGS," in *",$IMAGE,"*) printf yes ;; *) printf 'no(%s)' "$IMAGE_TAGS" ;; esac)"
info "image"  "$IMAGE  id=${IMAGE_ID#sha256:}"
info "run"    "$RUN   workdir=$W (0700, files 0600, shredded on exit)"
[[ -z "$SABOTAGE" ]] || printf '\n  !!!!!!  SABOTAGE ACTIVE: scenario %q will receive the CORRECT key  !!!!!!\n' "$SABOTAGE"

# ── volume / boot primitives ────────────────────────────────────────────────
prepare_cfg(){ # <volume> <variation>
  docker run --rm --network none -v "$1:/cfg" -v "$W:/w:ro" --entrypoint sh "$IMAGE" -c "
    set -e
    tar -xzf /w/cfg.tar.gz -C /tmp
    cp -a /tmp/postgresql-custom/. /cfg/
    chown -R postgres:postgres /cfg
    K=/cfg/pgsodium_root.key
    case '$2' in
      correct)    cp /w/rootkey.hex \$K; chown postgres:postgres \$K; chmod 600 \$K ;;
      absent)     : ;;
      wrongkey)   head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n' > \$K
                  chown postgres:postgres \$K; chmod 600 \$K ;;
      wrongowner) cp /w/rootkey.hex \$K; chown root:root         \$K; chmod 600 \$K ;;
      mode644)    cp /w/rootkey.hex \$K; chown postgres:postgres \$K; chmod 644 \$K ;;
      mode000)    cp /w/rootkey.hex \$K; chown postgres:postgres \$K; chmod 000 \$K ;;
      wrongpath)  cp /w/rootkey.hex /cfg/pgsodium-root.key
                  chown postgres:postgres /cfg/pgsodium-root.key; chmod 600 /cfg/pgsodium-root.key ;;
      *) echo 'unknown variation' >&2; exit 9 ;;
    esac" >/dev/null
}

# "<owner>:<group> <mode> <sha16>" | "ABSENT" — the key itself is never emitted
key_stat(){ # <volume> <filename>
  docker run --rm --network none -v "$1:/cfg:ro" --entrypoint sh "$IMAGE" -c "
    f=/cfg/$2
    if [ ! -e \$f ]; then echo ABSENT; exit 0; fi
    printf '%s %s ' \"\$(stat -c '%U:%G' \$f)\" \"\$(stat -c '%a' \$f)\"
    sha256sum \$f | cut -c1-16" 2>/dev/null || printf 'ERR:key_stat'
}
key_sha(){ local k; k="$(key_stat "$1" "$2")"; printf '%s' "${k##* }"; }
key_present(){ local k; k="$(key_stat "$1" "$2")"; if [[ "$k" == ABSENT ]]; then printf no; else printf yes; fi; }

boot(){ # <cn> <vol>
  docker run -d --name "$1" --network none -v "$2:/etc/postgresql-custom" \
    -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" "$IMAGE" >/dev/null
}

# ── fixture ─────────────────────────────────────────────────────────────────
hdr "fixture: a database with supabase_vault, $VAULT_N secrets, and an unrelated canary"

SRC="ntc2-$RUN-src"; SRCVOL="ntc2v-$RUN-src"
CONTAINERS+=("$SRC"); VOLUMES+=("$SRCVOL")

docker run --rm --network none --entrypoint tar "$IMAGE" -czf - -C /etc postgresql-custom > "$W/cfg.tar.gz"
assert_eq "pristine /etc/postgresql-custom captured" "yes" \
  "$(if [[ -s "$W/cfg.tar.gz" ]]; then printf yes; else printf no; fi)"

# 64 lowercase hex characters, no trailing newline: byte-for-byte the shape
# pgsodium_getkey.sh itself produces. Never printed.
head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n' > "$W/rootkey.hex"
chmod 600 "$W/rootkey.hex"
ROOTKEY_SHA="$(sha256sum < "$W/rootkey.hex" | cut -d' ' -f1)"
ROOTKEY_SHA16="${ROOTKEY_SHA:0:16}"
assert_eq "root key is 64 bytes of hex" "64" "$(stat -c %s "$W/rootkey.hex")"
info "root key digest" "$ROOTKEY_SHA16…  (the key itself is never displayed)"

docker volume create "$SRCVOL" >/dev/null
prepare_cfg "$SRCVOL" correct
boot "$SRC" "$SRCVOL"
if wait_ready "$SRC"; then
  pass "fixture source semantically ready" "${READY_S}s, $READY_STREAK consecutive queries"
else
  fail "fixture source semantically ready" "state=$READY_STATE after ${READY_S}s"
  echo "FATAL: cannot build a fixture on a server that never became ready"; exit 1
fi
assert_eq "fixture source runs the pinned image" "$IMAGE_ID"     "$(img_of "$SRC")"
assert_eq "fixture source uses OUR root key"     "$ROOTKEY_SHA16" "$(key_sha "$SRCVOL" pgsodium_root.key)"
assert_eq "supabase_vault present in the fixture" "0.3.1" \
  "$(dsql "$SRC" postgres "SELECT extversion FROM pg_extension WHERE extname='supabase_vault'")"

# A role a fresh image does NOT have, owning the canary table. This is what makes
# "globals reconciled" load-bearing instead of a no-op assertion: without globals
# the restore cannot succeed at all.
assert_eq "fixture role created"  "0" "$(dsql_rc "$SRC" postgres "CREATE ROLE $FIX_ROLE NOLOGIN")"
assert_eq "canary table created"  "0" "$(dsql_rc "$SRC" postgres \
  "CREATE TABLE public.c2_canary(id int PRIMARY KEY, payload text NOT NULL)")"
assert_eq "canary populated"      "0" "$(dsql_rc "$SRC" postgres \
  "INSERT INTO public.c2_canary SELECT g, md5(g::text) FROM generate_series(1,$CANARY_N) g")"
assert_eq "canary owned by the fixture role" "0" "$(dsql_rc "$SRC" postgres \
  "ALTER TABLE public.c2_canary OWNER TO $FIX_ROLE")"

# Two secrets. openssl writes to stdout and tr strips the newline, so the
# plaintexts never pass through any argv; the SQL carrying them is assembled with
# shell builtins only into a mode-0600 file, streamed to psql on STDIN, then
# shredded. Comparison later is by digest, never by value.
openssl rand -hex 24 | tr -d '\n' > "$W/s1.txt"; chmod 600 "$W/s1.txt"
openssl rand -hex 24 | tr -d '\n' > "$W/s2.txt"; chmod 600 "$W/s2.txt"
{
  printf "SELECT vault.create_secret(%s,'nt-c2-alpha','c2 fixture secret one');\n" "$(sql_lit "$W/s1.txt")"
  printf "SELECT vault.create_secret(%s,'nt-c2-beta','c2 fixture secret two');\n"  "$(sql_lit "$W/s2.txt")"
} > "$W/mksecrets.sql"
chmod 600 "$W/mksecrets.sql"
# Positive control on the GENERATOR before the generated SQL is trusted: two
# statements, each carrying a 48-hex literal. Asserted by shape, so the check
# never prints or transmits the value it is checking.
assert_eq "secret SQL generated with both literals intact" "2" \
  "$(n=0; rc=0; n="$(grep -cE "^SELECT vault\.create_secret\('[0-9a-f]{48}','nt-c2-" "$W/mksecrets.sql")" || rc=$?
     if [[ $rc -gt 1 ]]; then printf SCAN_BROKEN; else printf '%s' "$n"; fi)"
MK_RC=0
docker exec -i "$SRC" psql -U supabase_admin -d postgres -X -A -t -q -v ON_ERROR_STOP=1 -f - \
  < "$W/mksecrets.sql" >/dev/null 2>"$W/mk.err" || MK_RC=$?
shred -u "$W/mksecrets.sql" >/dev/null 2>&1 || rm -f "$W/mksecrets.sql"
assert_eq "secrets created via stdin (never argv)" "0" "$MK_RC"

EXP_DIGESTS="$(sha256sum < "$W/s1.txt" | cut -d' ' -f1) $(sha256sum < "$W/s2.txt" | cut -d' ' -f1)"
DIGEST_SQL="SELECT encode(sha256(convert_to(decrypted_secret,'UTF8')),'hex') FROM vault.decrypted_secrets ORDER BY name"
CANARY_SQL="SELECT encode(sha256(convert_to(string_agg(id||':'||payload,'|' ORDER BY id),'UTF8')),'hex') FROM public.c2_canary"
EXT_SQL="SELECT string_agg(extname||'='||extversion,',' ORDER BY extname) FROM pg_extension"
OWNER_SQL="SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.c2_canary'::regclass"
CIPHER_SQL="SELECT count(*) FROM vault.secrets WHERE secret IS NOT NULL AND length(secret) > 0"
DECN_SQL="SELECT count(*) FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL"

assert_eq "fixture: vault ciphertext rows"     "$VAULT_N" "$(dsql "$SRC" postgres "$CIPHER_SQL")"
assert_eq "fixture: secrets decrypt AT SOURCE" "$VAULT_N" "$(dsql "$SRC" postgres "$DECN_SQL")"
assert_eq "fixture: source plaintexts are the ones we wrote" "$EXP_DIGESTS" \
  "$(dsql_join "$SRC" postgres "$DIGEST_SQL")"
assert_eq "fixture: canary rows" "$CANARY_N" "$(dsql "$SRC" postgres 'SELECT count(*) FROM public.c2_canary')"
CANARY_DIGEST="$(dsql "$SRC" postgres "$CANARY_SQL")"
EXT_LIST="$(dsql "$SRC" postgres "$EXT_SQL")"
SRC_ROLES="$(dsql "$SRC" postgres 'SELECT count(*) FROM pg_roles')"
info "fixture canary digest" "${CANARY_DIGEST:0:16}…"
info "fixture roles"         "$SRC_ROLES"

# globals, made idempotent exactly the way nt-restore-drill.sh does it
docker exec "$SRC" pg_dumpall -U supabase_admin --globals-only > "$W/globals.sql" 2>"$W/globals.dump.err"
chmod 600 "$W/globals.sql"
if scanner_ok "globals" "$W/globals.sql" 'CREATE ROLE'; then
  assert_eq "globals carry the fixture role" "yes" "$(log_has "$W/globals.sql" "CREATE ROLE $FIX_ROLE;")"
fi
python3 - "$W/globals.sql" "$W/globals_idem.sql" <<'PY'
import re, sys
out = []
for line in open(sys.argv[1], encoding='utf-8', errors='replace').read().splitlines(True):
    m = re.match(r'^CREATE ROLE (\S+);', line.strip())
    if m:
        r = m.group(1)
        out.append("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='%s') "
                   "THEN CREATE ROLE %s; END IF; END $$;\n" % (r.strip('"'), r))
    else:
        out.append(line)
open(sys.argv[2], 'w').write(''.join(out))
PY
chmod 600 "$W/globals_idem.sql"

docker exec "$SRC" pg_dump -U supabase_admin -Fc -Z6 -f /tmp/c2.dump postgres 2>"$W/pgdump.err"
docker cp "$SRC:/tmp/c2.dump" "$W/c2.dump" >/dev/null
chmod 600 "$W/c2.dump"
assert_eq "fixture archive produced" "yes" \
  "$(if [[ -s "$W/c2.dump" ]]; then printf yes; else printf no; fi)"
docker run --rm --network none -v "$W:/w:ro" "$IMAGE" pg_restore -l /w/c2.dump > "$W/toc.txt" 2>"$W/toc.err"
if scanner_ok "archive TOC" "$W/toc.txt" '; Archive created at'; then
  assert_eq "archive declares vault TABLE DATA" "yes" "$(log_has "$W/toc.txt" 'TABLE DATA vault secrets')"
fi
# A TOC entry is a DECLARATION (audit C-4). Full consumption is the only honest
# statement that the fixture's data bytes are actually there.
CONSUME_RC=0
docker run --rm --network none -v "$W:/w:ro" "$IMAGE" pg_restore -f /dev/null /w/c2.dump \
  >/dev/null 2>"$W/consume.err" || CONSUME_RC=$?
assert_eq "archive is fully consumable end to end" "0" "$CONSUME_RC"
info "archive" "$(stat -c %s "$W/c2.dump") bytes"

# The probe always returns a value, so an error can never be read as "zero rows"
# and an empty string can never be read as anything at all.
cat > "$W/probe.sql" <<'SQL'
CREATE OR REPLACE FUNCTION pg_temp.c2probe() RETURNS text LANGUAGE plpgsql AS $f$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM vault.decrypted_secrets WHERE decrypted_secret IS NOT NULL;
  RETURN 'decrypted=' || n;
EXCEPTION WHEN OTHERS THEN
  RETURN 'sqlstate=' || SQLSTATE || ' msg=' || left(replace(SQLERRM, chr(10), ' '), 200);
END
$f$;
SELECT pg_temp.c2probe();
SQL
probe(){ # <cn> <db>
  local out rc=0
  out="$(docker exec -i "$1" psql -U supabase_admin -d "$2" -X -A -t -q -v ON_ERROR_STOP=1 -f - \
        < "$W/probe.sql" 2>"$W/probe.err")" || rc=$?
  if [[ $rc -ne 0 ]]; then printf 'ERR:probe_exec'; return 0; fi
  if [[ -z "$out" ]]; then printf 'ERR:probe_empty'; return 0; fi
  printf '%s' "$out"
}

# Observed classes, not guessed ones.
EXPECT_SQLSTATE='22000'
EXPECT_MSG='pgsodium_crypto_aead_det_decrypt_by_id: invalid ciphertext'
EXPECT_BOOT_DENIED="pgsodium_root.key': Permission denied"
EXPECT_BOOT_FATAL='FATAL:  invalid secret key'
BOOTLOG_CONTROL='Success. You can now start the database server using:'

# ── the prerequisite chain ──────────────────────────────────────────────────
prereqs(){ # <label> <cn> -> 0 only if EVERY prerequisite passed
  local lab="$1" cn="$2" before=$BAD rc

  assert_eq "$lab P1 runs the pinned image ID" "$IMAGE_ID" "$(img_of "$cn")"

  assert_eq "$lab P3 fixture role absent before globals" "0" \
    "$(dsql "$cn" postgres "SELECT count(*) FROM pg_roles WHERE rolname='$FIX_ROLE'")"
  docker cp "$W/globals_idem.sql" "$cn:/tmp/globals_idem.sql" >/dev/null
  rc=0
  docker exec "$cn" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q -f /tmp/globals_idem.sql \
    >/dev/null 2>"$W/g.err" || rc=$?
  assert_eq "$lab P3 globals applied cleanly"        "0" "$rc"
  assert_eq "$lab P3 fixture role present after"     "1" \
    "$(dsql "$cn" postgres "SELECT count(*) FROM pg_roles WHERE rolname='$FIX_ROLE'")"
  assert_eq "$lab P3 role count matches the source"  "$SRC_ROLES" "$(dsql "$cn" postgres 'SELECT count(*) FROM pg_roles')"

  assert_eq "$lab P4 fresh target database created"  "0" \
    "$(dsql_rc "$cn" postgres "CREATE DATABASE $TARGET_DB OWNER supabase_admin")"
  assert_eq "$lab P4 target empty before restore"    "0" \
    "$(dsql "$cn" "$TARGET_DB" "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r'")"

  docker cp "$W/c2.dump" "$cn:/tmp/c2.dump" >/dev/null
  rc=0
  docker exec "$cn" pg_restore -U supabase_admin -d "$TARGET_DB" --exit-on-error --verbose /tmp/c2.dump \
    >/dev/null 2>"$W/restore.err" || rc=$?
  assert_eq "$lab P5 pg_restore --exit-on-error"     "0" "$rc"
  assert_eq "$lab P5 zero pg_restore error lines"    "0" "$(count_lines_with "$W/restore.err" 'pg_restore: error')"

  assert_eq "$lab P6 vault.secrets ciphertext rows"  "$VAULT_N" "$(dsql "$cn" "$TARGET_DB" 'SELECT count(*) FROM vault.secrets')"
  assert_eq "$lab P6 every row carries ciphertext"   "$VAULT_N" "$(dsql "$cn" "$TARGET_DB" "$CIPHER_SQL")"

  assert_eq "$lab P7 canary rows"                    "$CANARY_N"      "$(dsql "$cn" "$TARGET_DB" 'SELECT count(*) FROM public.c2_canary')"
  assert_eq "$lab P7 canary content digest"          "$CANARY_DIGEST" "$(dsql "$cn" "$TARGET_DB" "$CANARY_SQL")"
  assert_eq "$lab P7 canary owner"                   "$FIX_ROLE"      "$(dsql "$cn" "$TARGET_DB" "$OWNER_SQL")"
  assert_eq "$lab P7 extension set"                  "$EXT_LIST"      "$(dsql "$cn" "$TARGET_DB" "$EXT_SQL")"

  [[ $BAD -eq $before ]]
}

# ── scoring the one variable ────────────────────────────────────────────────
score_decrypts(){ # <label> <cn>
  local lab="$1" cn="$2"
  assert_eq "$lab ROOT KEY: secrets decrypt" "decrypted=$VAULT_N" "$(probe "$cn" "$TARGET_DB")"
  assert_eq "$lab ROOT KEY: plaintexts are the fixture's" "$EXP_DIGESTS" \
    "$(dsql_join "$cn" "$TARGET_DB" "$DIGEST_SQL")"
}
score_broken(){ # <label> <cn>
  local lab="$1" cn="$2" p state msg
  p="$(probe "$cn" "$TARGET_DB")"
  if [[ "$p" != sqlstate=* ]]; then
    fail "$lab ROOT KEY: decryption must fail" "probe returned '$p' — the key change did NOT protect the secrets"
    return 0
  fi
  state="${p#sqlstate=}"; state="${state%% *}"
  msg="${p#*msg=}"
  assert_eq "$lab ROOT KEY: SQLSTATE"      "$EXPECT_SQLSTATE" "$state"
  assert_eq "$lab ROOT KEY: failure class" "yes" \
    "$(case "$msg" in *"$EXPECT_MSG"*) printf yes ;; *) printf 'no(%s)' "$msg" ;; esac)"
  # and no plaintext is reachable by any other route
  assert_eq "$lab ROOT KEY: no plaintext obtainable" "ERR:sql_exec" "$(dsql "$cn" "$TARGET_DB" "$DIGEST_SQL")"
}

# ── the scenario driver ─────────────────────────────────────────────────────
want(){ [[ -z "$ONLY" ]] && return 0; case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
sabotaged(){ [[ -n "$SABOTAGE" && "$SABOTAGE" == "$1" ]]; }
tally_sabotage(){ if sabotaged "$1"; then SABOTAGE_HIT=$((SABOTAGE_HIT + BAD - $2)); fi; }

scenario(){ # <slug> <title> <variation> <expect: decrypt|nodecrypt|noboot>
  local slug="$1" title="$2" var="$3" expect="$4"
  want "$slug" || return 0
  local before=$BAD cn="ntc2-$RUN-$slug" vol="ntc2v-$RUN-$slug" lab="[$slug]" ready=0

  if sabotaged "$slug"; then
    hdr "SCENARIO $slug — $title   *** SABOTAGED: installing the CORRECT key ***"
    var=correct
  else
    hdr "SCENARIO $slug — $title"
  fi

  CONTAINERS+=("$cn"); VOLUMES+=("$vol")
  docker volume create "$vol" >/dev/null
  prepare_cfg "$vol" "$var"
  info "$lab key file before boot" "$(key_stat "$vol" pgsodium_root.key)"
  boot "$cn" "$vol"
  wait_ready "$cn" || ready=$?

  if [[ "$expect" == noboot ]]; then
    dump_logs "$cn" "$W/$slug.log"
    if scanner_ok "$lab boot log" "$W/$slug.log" "$BOOTLOG_CONTROL"; then
      assert_eq "$lab log: key unreadable by postgres" "yes" "$(log_has "$W/$slug.log" "$EXPECT_BOOT_DENIED")"
      assert_eq "$lab log: exact refusal"              "yes" "$(log_has "$W/$slug.log" "$EXPECT_BOOT_FATAL")"
    fi
    assert_eq "$lab postmaster refused to start" "left-running" \
      "$(if [[ $ready -eq 1 ]]; then printf left-running; else printf 'ready=%s,state=%s' "$ready" "$READY_STATE"; fi)"
    assert_eq "$lab container state"     "exited" "$(state_of "$cn")"
    assert_eq "$lab container exit code" "1"      "$(exitcode_of "$cn")"

    # REPAIR CONTROL — change back ONLY the varied property, on the SAME config
    # volume, and require the whole happy path. Without this, "it did not boot"
    # is attributable to anything at all, which is the original defect in a hat.
    local rcn="ntc2-$RUN-$slug-repair"
    CONTAINERS+=("$rcn")
    case "$var" in
      wrongowner) docker run --rm --network none -v "$vol:/cfg" --entrypoint sh "$IMAGE" -c 'chown postgres:postgres /cfg/pgsodium_root.key' >/dev/null ;;
      mode000)    docker run --rm --network none -v "$vol:/cfg" --entrypoint sh "$IMAGE" -c 'chmod 600 /cfg/pgsodium_root.key' >/dev/null ;;
      correct)    info "$lab repair" "sabotage path: the key was already correct, nothing to repair" ;;
      *)          fail "$lab repair control" "no repair defined for variation $var" ;;
    esac
    info "$lab key file after repair" "$(key_stat "$vol" pgsodium_root.key)"
    boot "$rcn" "$vol"
    if wait_ready "$rcn"; then
      pass "$lab REPAIR: same volume, one property fixed, server starts" "${READY_S}s"
      if prereqs "$lab-repair" "$rcn"; then
        score_decrypts "$lab-repair" "$rcn"
      else
        fail "$lab-repair NOT SCORED" "a prerequisite failed; attribution unproven"
      fi
    else
      fail "$lab REPAIR: server starts once the property is fixed" \
           "state=$READY_STATE after ${READY_S}s — the boot failure was NOT attributable to the key"
    fi
    tally_sabotage "$slug" "$before"
    return 0
  fi

  if [[ $ready -eq 0 ]]; then
    pass "$lab P2 semantic readiness ($READY_STREAK consecutive queries)" "${READY_S}s"
  else
    fail "$lab P2 semantic readiness" "state=$READY_STATE after ${READY_S}s"
    dump_logs "$cn" "$W/$slug.log"
    tail -n 25 "$W/$slug.log" | sed 's/^/        /'
    tally_sabotage "$slug" "$before"
    return 0
  fi
  info "$lab key file after boot" "$(key_stat "$vol" pgsodium_root.key)"

  # scenario-specific observations, asserted rather than narrated
  case "$slug" in
    absent|wrongpath)
      if [[ "$var" == correct ]]; then
        info "$lab regeneration check skipped" "sabotage installed the correct key"
      else
        assert_eq "$lab canonical key exists after boot" "yes" "$(key_present "$vol" pgsodium_root.key)"
        assert_ne "$lab canonical key was SILENTLY REGENERATED" "$ROOTKEY_SHA16" \
          "$(key_sha "$vol" pgsodium_root.key)" "on-disk key is not the captured one"
        finding "boot minted a fresh random root key at the canonical path and the cluster came up healthy"
      fi
      if [[ "$slug" == wrongpath && "$var" != correct ]]; then
        assert_eq "$lab the operator's key is present but unused" "$ROOTKEY_SHA16" \
          "$(key_sha "$vol" pgsodium-root.key)"
      fi
      ;;
  esac

  if ! prereqs "$lab" "$cn"; then
    fail "$lab ROOT KEY NOT SCORED" "a prerequisite failed — an unrelated failure must never satisfy this control"
    tally_sabotage "$slug" "$before"
    return 0
  fi

  case "$expect" in
    decrypt)   score_decrypts "$lab" "$cn" ;;
    nodecrypt) score_broken   "$lab" "$cn" ;;
  esac

  if [[ "$slug" == mode644 && "$var" == mode644 ]]; then
    finding "mode 0644 decrypts perfectly — file MODE is not an enforced control; only readability by the postgres user is"
  fi

  tally_sabotage "$slug" "$before"
  return 0
}

# ── fixture gate ────────────────────────────────────────────────────────────
# Scenarios are statements about a fixture. If the fixture is not sound, running
# them produces a page of confident-looking output about nothing — which is the
# exact failure mode this whole file exists to end. Stop here instead.
if [[ $BAD -ne 0 ]]; then
  hdr "FATAL: the fixture is not sound ($BAD failed fixture assertion(s))"
  echo "  Refusing to run scenarios: every result would be a statement about a broken fixture."
  exit 1
fi
pass "fixture is sound — scenarios may be scored" "$OK assertions, 0 failures"

# ── scenarios 1..6 ──────────────────────────────────────────────────────────
# (1) is mandatory. Without it the suite could pass on a database where nothing
# ever decrypts, and every other scenario would be satisfied for free.
scenario correct    "POSITIVE CONTROL — correct key, installed pre-boot"  correct    decrypt
scenario absent     "key absent"                                          absent     nodecrypt
scenario wrongkey   "valid-format but WRONG 64-hex key"                   wrongkey   nodecrypt
scenario wrongowner "wrong file OWNER (root:root, mode 0600)"             wrongowner noboot
scenario mode644    "wrong file MODE (0644, owner postgres)"              mode644    decrypt
scenario mode000    "wrong file MODE (0000, owner postgres)"              mode000    noboot
scenario wrongpath  "key present under the WRONG FILENAME"                wrongpath  nodecrypt

# ── scenario 7: correct key installed only AFTER first boot ─────────────────
if want late; then
  BEFORE_LATE=$BAD
  LATE_CN="ntc2-$RUN-late"; LATE_VOL="ntc2v-$RUN-late"; LATE_LAB='[late]'
  if sabotaged late; then
    hdr "SCENARIO late — correct key installed only AFTER first boot   *** SABOTAGED: correct key pre-boot ***"
    LATE_VAR=correct
  else
    hdr "SCENARIO late — correct key installed only AFTER first boot"
    LATE_VAR=absent
  fi
  CONTAINERS+=("$LATE_CN"); VOLUMES+=("$LATE_VOL")
  docker volume create "$LATE_VOL" >/dev/null
  prepare_cfg "$LATE_VOL" "$LATE_VAR"
  info "$LATE_LAB key file before boot" "$(key_stat "$LATE_VOL" pgsodium_root.key)"
  boot "$LATE_CN" "$LATE_VOL"
  LATE_READY=0; wait_ready "$LATE_CN" || LATE_READY=$?
  if [[ $LATE_READY -ne 0 ]]; then
    fail "$LATE_LAB P2 semantic readiness" "state=$READY_STATE after ${READY_S}s"
  else
    pass "$LATE_LAB P2 semantic readiness ($READY_STREAK consecutive queries)" "${READY_S}s"
    info "$LATE_LAB key at boot" "$(key_stat "$LATE_VOL" pgsodium_root.key)"
    if prereqs "$LATE_LAB" "$LATE_CN"; then
      # (a) before the correct key is installed
      if [[ "$LATE_VAR" == correct ]]; then
        info "$LATE_LAB pre-install probe" "$(probe "$LATE_CN" "$TARGET_DB")  (sabotage: correct key was already in place)"
        assert_eq "$LATE_LAB must NOT decrypt before the key is installed" "nodecrypt" \
          "$(p="$(probe "$LATE_CN" "$TARGET_DB")"; if [[ "$p" == sqlstate=* ]]; then printf nodecrypt; else printf '%s' "$p"; fi)"
      else
        score_broken "$LATE_LAB-before" "$LATE_CN"
      fi
      # (b) correct key installed at runtime — vary ONLY the timing, no restart
      docker run --rm --network none -v "$LATE_VOL:/cfg" -v "$W:/w:ro" --entrypoint sh "$IMAGE" -c \
        'cp /w/rootkey.hex /cfg/pgsodium_root.key; chown postgres:postgres /cfg/pgsodium_root.key; chmod 600 /cfg/pgsodium_root.key' >/dev/null
      assert_eq "$LATE_LAB correct key now on disk" "$ROOTKEY_SHA16" "$(key_sha "$LATE_VOL" pgsodium_root.key)"
      if [[ "$LATE_VAR" == correct ]]; then
        info "$LATE_LAB post-install probe" "$(probe "$LATE_CN" "$TARGET_DB")"
      else
        score_broken "$LATE_LAB-installed-no-restart" "$LATE_CN"
        finding "the root key is read ONCE at postmaster start; writing the correct key at runtime changes nothing"
      fi
      # (c) restart — vary only that
      docker restart "$LATE_CN" >/dev/null
      if wait_ready "$LATE_CN"; then
        pass "$LATE_LAB restarted and semantically ready" "${READY_S}s"
        assert_eq "$LATE_LAB still the pinned image after restart" "$IMAGE_ID" "$(img_of "$LATE_CN")"
        assert_eq "$LATE_LAB ciphertext rows survived the restart" "$VAULT_N"      "$(dsql "$LATE_CN" "$TARGET_DB" "$CIPHER_SQL")"
        assert_eq "$LATE_LAB canary digest survived the restart"   "$CANARY_DIGEST" "$(dsql "$LATE_CN" "$TARGET_DB" "$CANARY_SQL")"
        score_decrypts "$LATE_LAB-after-restart" "$LATE_CN"
      else
        fail "$LATE_LAB restarted and semantically ready" "state=$READY_STATE after ${READY_S}s"
      fi
    else
      fail "$LATE_LAB ROOT KEY NOT SCORED" "a prerequisite failed — an unrelated failure must never satisfy this control"
    fi
  fi
  tally_sabotage late "$BEFORE_LATE"
fi

hdr "findings"
info "F1" "mode 0644 DECRYPTS — file mode is not an enforced control, only readability by postgres is"
info "F2" "absent / misnamed key is SILENT — a fresh random key is generated and persisted, cluster looks healthy"
info "F3" "the root key is read once at postmaster start — a late install needs a restart"
info "scenarios" "${ONLY:-correct,absent,wrongkey,wrongowner,mode644,mode000,wrongpath,late}"

exit 0
