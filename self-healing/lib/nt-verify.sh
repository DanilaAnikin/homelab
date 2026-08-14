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
