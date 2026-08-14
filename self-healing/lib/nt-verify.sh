# shellcheck shell=bash
# ============================================================================
# nt-verify.sh — strict typed verification primitives.
#
# WHY THIS EXISTS (audit finding H-1)
# -----------------------------------
# The previous drill compared two strings:
#
#     check(){ if [[ "$2" == "$3" ]]; then PASS=…; else FAIL=…; fi }
#
# Both arguments were produced by command substitution, and command
# substitution failures do NOT trip `set -e` when they appear in the argument
# list of a simple command. So every one of these produced the empty string
# and then compared equal to another empty string:
#
#   * a missing manifest key                 (python KeyError -> "" on stdout)
#   * a corrupt/truncated MANIFEST.json      (json.load raises -> "")
#   * psql unable to connect                 ("" on stdout)
#   * a SQL syntax error                     ("" on stdout)
#   * SQL NULL under -At                     (rendered as "")
#   * string_agg over an empty relation      (NULL -> "")
#
# Eleven call sites were reachable this way. `check "" ""` scored PASS, the
# failure counter stayed at zero, and the drill printed DRILL PASSED. The count
# was real; it measured nothing.
#
# THE REPLACEMENT
# ---------------
# Nothing compares bare strings any more. Every producer returns a *typed
# record*: a status, a declared type, and a value. A comparison may pass only
# when both sides executed successfully, agree on type, satisfy that type's
# shape, are non-empty, and are equal. Anything else is a FAIL with a named
# reason — never a silent equality of two absences.
#
# An empty value can still be the property under test, but only by asking for
# it explicitly with the `text0` type. It is never the accidental default.
# ============================================================================

# Unit separator: cannot occur in psql -A output or in JSON we emit.
NTV_RS=$'\x1f'
# Sentinel psql prints for SQL NULL, so NULL is distinguishable from '' and
# from "no rows at all". Chosen to be impossible as a real catalogue value.
NTV_NULL='__NTV_NULL__'

NTV_PASS=0
NTV_FAIL=0
NTV_FIRST_FAILURE=''
NTV_STAGE='init'

ntv_stage(){ NTV_STAGE="$1"; }

# ── record construction / parsing ───────────────────────────────────────────
# A record is: <status><RS><type><RS><value>
#   status: "ok" | "err:<class>"
# Values may contain newlines; parsing uses parameter expansion, never `cut`.

ntv_ok(){  printf '%s%s%s%s%s' 'ok'      "$NTV_RS" "$1" "$NTV_RS" "$2"; }
ntv_bad(){ printf '%s%s%s%s%s' "err:$1"  "$NTV_RS" "$2" "$NTV_RS" ''; }

# ntv_parse <record> — sets NTV_STATUS / NTV_TYPE / NTV_VALUE
ntv_parse(){
  local rec="$1"
  NTV_STATUS="${rec%%"$NTV_RS"*}"; rec="${rec#*"$NTV_RS"}"
  NTV_TYPE="${rec%%"$NTV_RS"*}"
  NTV_VALUE="${rec#*"$NTV_RS"}"
}

# ── shape validation ────────────────────────────────────────────────────────
# A declared type is a promise about the value's *form*. A count that comes
# back as "ERROR:  relation does not exist" is not an int, and must not be
# allowed to compare equal to anything just because both sides broke.
ntv_shape_ok(){ # ntv_shape_ok <type> <value>
  case "$1" in
    int)     [[ "$2" =~ ^-?[0-9]+$ ]] ;;
    bool_t)  [[ "$2" == 't' || "$2" == 'f' ]] ;;
    sha256)  [[ "$2" =~ ^[0-9a-f]{64}$ ]] ;;
    json)    printf '%s' "$2" | python3 -c 'import json,sys;json.loads(sys.stdin.read())' 2>/dev/null ;;
    text)    [[ -n "$2" ]] ;;
    text0)   return 0 ;;   # documented-possibly-empty; opt-in only
    *)       return 1 ;;
  esac
}

# ── the gate ────────────────────────────────────────────────────────────────
ntv_check(){ # ntv_check <name> <expected_record> <actual_record>
  local name="$1" es et ev as at av why=''
  ntv_parse "$2"; es="$NTV_STATUS"; et="$NTV_TYPE"; ev="$NTV_VALUE"
  ntv_parse "$3"; as="$NTV_STATUS"; at="$NTV_TYPE"; av="$NTV_VALUE"

  if   [[ "$es" != ok ]];                       then why="expected-side did not execute ($es)"
  elif [[ "$as" != ok ]];                       then why="actual-side did not execute ($as)"
  elif [[ "$et" != "$at" ]];                    then why="type mismatch (expected $et, actual $at)"
  elif [[ "$et" != text0 && -z "$ev" ]];        then why="expected value is empty"
  elif [[ "$at" != text0 && -z "$av" ]];        then why="actual value is empty"
  elif ! ntv_shape_ok "$et" "$ev";              then why="expected value is not a valid $et"
  elif ! ntv_shape_ok "$at" "$av";              then why="actual value is not a valid $at"
  elif [[ "$ev" != "$av" ]];                    then why="value mismatch"
  fi

  if [[ -z "$why" ]]; then
    NTV_PASS=$((NTV_PASS+1)); printf '  PASS  %-46s %s\n' "$name" "$av"
  else
    NTV_FAIL=$((NTV_FAIL+1))
    [[ -n "$NTV_FIRST_FAILURE" ]] || NTV_FIRST_FAILURE="[$NTV_STAGE] $name: $why"
    printf '  FAIL  %-46s %s (expected=%q actual=%q)\n' "$name" "$why" "$ev" "$av"
  fi
}

# ── strict JSON / manifest accessor (replaces mj) ───────────────────────────
# Distinguishes, as separate error classes: unreadable file, unparseable JSON,
# missing key, SQL/JSON null, and wrong type. The old mj() collapsed all of
# these to "".
ntv_json(){ # ntv_json <file> <python-expr-over-d> <type>
  local f="$1" expr="$2" ty="$3" out st val
  out="$(NTV_EXPR="$expr" NTV_TY="$ty" python3 - "$f" <<'PY' 2>/dev/null
import json, os, sys
def emit(status, value=""):
    sys.stdout.write(status + "\n" + value)
    sys.exit(0)
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except FileNotFoundError:
    emit("err:no_file")
except (ValueError, UnicodeDecodeError):
    emit("err:json_parse")
except OSError:
    emit("err:io")
try:
    v = eval(os.environ["NTV_EXPR"], {"__builtins__": {}}, {"d": d})
except (KeyError, IndexError):
    emit("err:missing_key")
except Exception:
    emit("err:expr")
if v is None:
    emit("err:null")
ty = os.environ["NTV_TY"]
if ty == "int":
    if isinstance(v, bool) or not isinstance(v, int):
        emit("err:type")
    emit("ok", str(v))
if ty == "bool_t":
    if not isinstance(v, bool):
        emit("err:type")
    emit("ok", "t" if v else "f")
if ty == "json":
    # canonical form so both sides of a comparison serialise identically
    emit("ok", json.dumps(v, sort_keys=True, separators=(",", ":")))
if not isinstance(v, str):
    emit("err:type")
if v == "" and ty != "text0":
    emit("err:empty")
emit("ok", v)
PY
)" || { ntv_bad python_failed "$ty"; return 0; }
  st="${out%%$'\n'*}"
  val="${out#*$'\n'}"
  [[ "$st" == ok ]] && { ntv_ok "$ty" "$val"; return 0; }
  ntv_bad "${st#err:}" "$ty"
}

# ── strict SQL accessor (replaces q / psqlc / mj-as-oracle) ─────────────────
# Contract for the caller-supplied runner:
#   ntv_psql_exec <sql>
#     MUST run psql with -X -A -t -q -v ON_ERROR_STOP=1 -P null="$NTV_NULL"
#     MUST write rows to stdout and diagnostics to stderr
#     MUST return psql's exit status unmodified (no pipes, no `|| true`)
ntv_define_psql(){ # ntv_define_psql <container> <db> [role]
  NTV_PSQL_CN="$1"; NTV_PSQL_DB="$2"; NTV_PSQL_ROLE="${3:-supabase_admin}"
  ntv_psql_exec(){
    docker exec "$NTV_PSQL_CN" psql -U "$NTV_PSQL_ROLE" -d "$NTV_PSQL_DB" \
      -X -A -t -q -v ON_ERROR_STOP=1 -P null="$NTV_NULL" -c "$1"
  }
}

# Same contract as ntv_sql, but against an explicitly named container and
# database rather than the one bound by ntv_define_psql. Needed by controls
# that must query a SECOND, deliberately-broken clone — the altered-root-key
# control in particular — without disturbing the binding used by every other
# assertion in the run.
ntv_sql_on(){ # ntv_sql_on <container> <db> <type> <sql>
  local cn="$1" db="$2" ty="$3" sql="$4" out rc errf
  errf="$(mktemp "${NTV_TMP:-${TMPDIR:-/tmp}}/ntv-sqlon.XXXXXX")"
  rc=0
  out="$(docker exec "$cn" psql -U "${NTV_PSQL_ROLE:-supabase_admin}" -d "$db" \
         -X -A -t -q -v ON_ERROR_STOP=1 -P null="$NTV_NULL" -c "$sql" 2>"$errf")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    if grep -qiE 'could not connect|connection refused|no such container|is not running' "$errf"; then
      ntv_bad sql_connect "$ty"
    else
      ntv_bad sql_exec "$ty"
    fi
    NTV_LAST_SQL_ERR="$(head -c 2000 "$errf")"; rm -f "$errf"; return 0
  fi
  rm -f "$errf"
  if [[ -z "$out" ]];             then ntv_bad no_rows  "$ty"; return 0; fi
  if [[ "$out" == "$NTV_NULL" ]]; then ntv_bad sql_null "$ty"; return 0; fi
  if ! ntv_shape_ok "$ty" "$out"; then ntv_bad shape    "$ty"; return 0; fi
  ntv_ok "$ty" "$out"
}

ntv_sql(){ # ntv_sql <type> <sql>
  local ty="$1" sql="$2" out rc errf
  errf="$(mktemp "${NTV_TMP:-${TMPDIR:-/tmp}}/ntv-sql.XXXXXX")"
  set +e
  out="$(ntv_psql_exec "$sql" 2>"$errf")"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    # classify so a control can assert *why* it failed, not merely that it did
    if grep -qiE 'could not connect|connection refused|no such container|is not running' "$errf"; then
      ntv_bad sql_connect "$ty"
    else
      ntv_bad sql_exec "$ty"
    fi
    NTV_LAST_SQL_ERR="$(head -c 2000 "$errf")"
    rm -f "$errf"; return 0
  fi
  NTV_LAST_SQL_ERR=''
  rm -f "$errf"
  # psql exited 0. Three distinct empty-ish outcomes must stay distinct:
  if [[ -z "$out" ]];              then ntv_bad no_rows  "$ty"; return 0; fi
  if [[ "$out" == "$NTV_NULL" ]];  then ntv_bad sql_null "$ty"; return 0; fi
  if [[ "$ty" != text0 && -z "${out//[[:space:]]/}" ]]; then ntv_bad empty "$ty"; return 0; fi
  if ! ntv_shape_ok "$ty" "$out";  then ntv_bad shape    "$ty"; return 0; fi
  ntv_ok "$ty" "$out"
}

# ── semantic readiness ──────────────────────────────────────────────────────
# `pg_isready` answers "is something listening", which is not the question.
# The supabase image bootstraps in stages and restarts the server partway
# through, so there is a window in which pg_isready reports success and every
# query still fails with "the database system is starting up". A single
# successful query can also land inside that window, just before the restart.
# Requiring N CONSECUTIVE round trips AS THE ROLE WE WILL ACTUALLY USE is what
# makes this stable.
ntv_wait_ready(){ # ntv_wait_ready <container> <db> <role> [consecutive] [timeout_s]
  local cn="$1" db="$2" role="$3" need="${4:-5}" budget="${5:-240}" streak=0 i
  for ((i=0; i<budget; i++)); do
    if docker exec "$cn" psql -U "$role" -d "$db" -X -Atqc 'SELECT 1' >/dev/null 2>&1; then
      streak=$((streak+1))
      if [[ $streak -ge $need ]]; then NTV_READY_AFTER="$i"; return 0; fi
    else
      streak=0
    fi
    sleep 1
  done
  return 1
}

# ── canonical catalogue (audit findings H-3, H-4) ───────────────────────────
# ONE function, called by the recovery-set builder against production and by
# the restore drill against the clone, streaming the SAME file into psql on
# both sides. That is what makes the two digests comparable at all: the
# previous code had two hand-written fingerprint queries that differed in their
# schema-exclusion escaping and in what they measured.
#
# Two outputs, deliberately separated:
#
#   NTV_CAT_SHA         sha256 over every line EXCEPT `pwverifier|`.
#                       This is the value that goes in the manifest.
#   <outfile>           the complete private stream, mode 0600, including the
#                       salted pwverifier lines. Never published, never
#                       uploaded, and the salt is fresh per run so it is not a
#                       stable commitment to anybody's password hash.
#
# A catalogue that silently lost its policy lines — because the query errored,
# or because the restore dropped every policy — must FAIL, not hash to
# something shorter and get compared to something else.
#
# The enforcement is NOT a hardcoded domain list. A fresh image legitimately
# has no triggers and no RLS policies, so a fixed list either fails on a valid
# empty database or has to be weakened until it proves nothing. The real
# property is equality WITH THE SOURCE: the builder records a per-domain line
# census in the manifest, and the drill asserts the clone's census matches it
# exactly. A restore that lost all 14 production policies then fails because
# the source said 14 and the clone says 0.
#
# The floor below is only for domains that cannot be absent from ANY live
# PostgreSQL database, and exists to catch a truncated or misparsed stream.
NTV_CAT_FLOOR_DOMAINS=(database schema rel role)

ntv_catalogue(){ # ntv_catalogue <container> <db> <role> <salt> <sqlfile> <outfile>
  local cn="$1" db="$2" role="$3" salt="$4" sqlf="$5" out="$6" rc=0 errf n
  NTV_CAT_SHA=''; NTV_CAT_LINES=0; NTV_CAT_ERR=''
  [[ -r "$sqlf" ]] || { NTV_CAT_ERR="catalogue SQL unreadable: $sqlf"; return 1; }
  errf="$(mktemp "${NTV_TMP:-${TMPDIR:-/tmp}}/ntv-cat.XXXXXX")"
  umask 077
  docker exec -i "$cn" psql -U "$role" -d "$db" -X -q \
      -v ON_ERROR_STOP=1 -v "ntv_pw_salt=$salt" -f - < "$sqlf" > "$out" 2>"$errf" || rc=$?
  NTV_CAT_STDERR="$(head -c 4000 "$errf")"; rm -f "$errf"
  chmod 600 "$out" 2>/dev/null || true

  if [[ $rc -ne 0 ]]; then NTV_CAT_ERR="psql exit $rc: $NTV_CAT_STDERR"; return 1; fi
  [[ -s "$out" ]] || { NTV_CAT_ERR="catalogue stream is empty"; return 1; }
  NTV_CAT_LINES="$(wc -l < "$out")"
  [[ "$NTV_CAT_LINES" -ge 100 ]] || { NTV_CAT_ERR="catalogue has only $NTV_CAT_LINES lines"; return 1; }

  # structural validity: every line is `domain|…`
  if grep -qvE '^[a-z]+\|' "$out"; then
    NTV_CAT_ERR="catalogue contains lines that are not domain-prefixed records"; return 1
  fi
  # floor domains: absence means the stream is truncated or misparsed
  local d missing=''
  for d in "${NTV_CAT_FLOOR_DOMAINS[@]}"; do
    grep -q "^$d|" "$out" || missing="$missing $d"
  done
  [[ -z "$missing" ]] || { NTV_CAT_ERR="catalogue is missing floor domains:$missing"; return 1; }

  n="$(grep -c '^pwverifier|' "$out" || true)"
  [[ "$n" -ge 1 ]] || { NTV_CAT_ERR="catalogue carries no pwverifier records"; return 1; }

  # the per-domain census the manifest records and the drill re-asserts, as
  # canonical JSON so both sides serialise identically
  NTV_CAT_CENSUS="$(grep -oE '^[a-z]+' "$out" | sort | uniq -c \
    | awk '{printf "%s\"%s\":%s", (NR>1?",":""), $2, $1}' | sed 's/^/{/; s/$/}/')"
  [[ -n "$NTV_CAT_CENSUS" ]] || { NTV_CAT_ERR="could not compute the domain census"; return 1; }

  NTV_CAT_SHA="$(grep -v '^pwverifier|' "$out" | sha256sum | cut -d' ' -f1)"
  [[ "$NTV_CAT_SHA" =~ ^[0-9a-f]{64}$ ]] || { NTV_CAT_ERR="digest is not a sha256"; return 1; }
  return 0
}

# ── private, set-bound password-verifier equality ───────────────────────────
# The old drill compared the clone's Vault plaintexts to LIVE PRODUCTION at
# drill time. That proves production and the clone agree right now; it does not
# prove the SET restores correctly, and it fails if production has legitimately
# moved on. The same mistake is available for password verifiers.
#
# This compares the clone against the recovery set's OWN globals component:
# pg_dumpall --globals-only carries `ALTER ROLE … PASSWORD 'SCRAM-SHA-256$…'`,
# so both sides of the comparison come from the set, under one fresh per-run
# salt, and nothing stable or plaintext is ever recorded.
ntv_pwverifier_matches_globals(){ # <catalogue_stream> <globals.sql> <salt>
  local cat="$1" glob="$2" salt="$3" tmp_exp tmp_act rc=0
  NTV_PW_ERR=''
  tmp_exp="$(mktemp "${NTV_TMP:-${TMPDIR:-/tmp}}/ntv-pwe.XXXXXX")"
  tmp_act="$(mktemp "${NTV_TMP:-${TMPDIR:-/tmp}}/ntv-pwa.XXXXXX")"
  chmod 600 "$tmp_exp" "$tmp_act"

  NTV_PW_SALT="$salt" python3 - "$glob" "$tmp_exp" <<'PY' || rc=$?
import hashlib, os, re, sys
salt = os.environ["NTV_PW_SALT"]
out = []
# pg_dumpall emits: ALTER ROLE name WITH ... PASSWORD 'verifier';
pat = re.compile(r"^ALTER ROLE (\S+) WITH .*?PASSWORD '([^']*)'", re.M)
for m in pat.finditer(open(sys.argv[1], encoding="utf-8", errors="replace").read()):
    role = m.group(1).strip('"')
    dig = hashlib.sha256(("ntv2pw:" + salt + ":" + role + ":" + m.group(2)).encode()).hexdigest()
    out.append(f"pwverifier|{role}|{dig}")
if not out:
    sys.stderr.write("no ALTER ROLE ... PASSWORD lines found in globals\n")
    sys.exit(2)
open(sys.argv[2], "w").write("\n".join(sorted(out)) + "\n")
PY
  if [[ $rc -ne 0 ]]; then
    rm -f "$tmp_exp" "$tmp_act"; NTV_PW_ERR="could not derive verifiers from globals (exit $rc)"; return 1
  fi

  # compare only the roles the globals actually carry a password for; roles
  # with no password have nothing to prove and must not silently pass as equal
  grep '^pwverifier|' "$cat" | sort > "$tmp_act"
  local nexp nmatch
  nexp="$(wc -l < "$tmp_exp")"
  nmatch="$(comm -12 "$tmp_exp" "$tmp_act" | wc -l)"
  rm -f "$tmp_exp" "$tmp_act"
  [[ "$nexp" -ge 1 ]] || { NTV_PW_ERR="globals carry no password verifiers"; return 1; }
  NTV_PW_EXPECTED="$nexp"; NTV_PW_MATCHED="$nmatch"
  [[ "$nmatch" -eq "$nexp" ]] || { NTV_PW_ERR="$nmatch of $nexp verifiers matched"; return 1; }
  return 0
}

# ── single-verdict discipline (audit finding H-7) ───────────────────────────
# The old script had two failure channels: counted FAILs, which wrote a
# verdict, and silent `set -e` aborts, which wrote nothing at all and produced
# no "DRILL FAILED" string for a wrapper to key on. Every exit path now lands
# in exactly one verdict.
NTV_VERDICT_FILE=''
NTV_VERDICT_WRITTEN=0
NTV_CLEANUP_HOOK=''

ntv_verdict_init(){ NTV_VERDICT_FILE="$1"; }

ntv_write_verdict(){ # ntv_write_verdict <exit_status>
  [[ -n "$NTV_VERDICT_FILE" ]] || return 0
  [[ $NTV_VERDICT_WRITTEN -eq 0 ]] || return 0
  NTV_VERDICT_WRITTEN=1
  local rc="$1" result cleanup_rc=0
  if [[ "$rc" -eq 0 && $NTV_FAIL -eq 0 && $NTV_PASS -gt 0 ]]; then result=PASS; else result=FAIL; fi
  if [[ -n "$NTV_CLEANUP_HOOK" ]]; then "$NTV_CLEANUP_HOOK" || cleanup_rc=$?; fi
  umask 077
  {
    echo "result=$result"
    echo "stage=$NTV_STAGE"
    echo "exit_status=$rc"
    echo "pass=$NTV_PASS"
    echo "fail=$NTV_FAIL"
    echo "first_failure=${NTV_FIRST_FAILURE:-none}"
    echo "cleanup_result=$cleanup_rc"
    echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$NTV_VERDICT_FILE"
  chmod 600 "$NTV_VERDICT_FILE" 2>/dev/null || true
}

# A bare `set -e` abort reaches ERR first, then EXIT. Recording the stage at
# ERR time is what turns a silent abort into a named failure.
ntv_on_err(){
  local rc=$?
  [[ -n "$NTV_FIRST_FAILURE" ]] || NTV_FIRST_FAILURE="[$NTV_STAGE] uncaught command failure (status $rc)"
  NTV_FAIL=$((NTV_FAIL+1))
}
ntv_on_exit(){ ntv_write_verdict "$?"; }

ntv_install_traps(){
  trap ntv_on_err ERR
  trap ntv_on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}
