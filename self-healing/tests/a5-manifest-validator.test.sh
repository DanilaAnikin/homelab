#!/usr/bin/env bash
# ============================================================================
# A-5 / C-3 — ONE manifest+marker validator, and controls that can actually
# fail.
#
# WHAT THIS REPLACES
# ------------------
# The legacy "manifest tamper detection" control wrote a modified manifest to
# a scratch file, computed its sha256, and asserted that the digest differed
# from the one recorded in COMPLETE. That is true by construction — a file
# whose bytes you chose does not hash to a digest you did not choose — so the
# assertion had no failing input. Measured: it still printed PASS with the real
# gate deleted from the drill outright, because the real gate was never on the
# path the control exercised.
#
# HOW EVERY CONTROL HERE IS BUILT
# -------------------------------
#   1. build a known-good set fixture on local disk (no network, no rclone, no
#      docker, no production access)
#   2. assert the UNRELATED PREREQUISITES hold — the pristine copy validates
#      cleanly through every stage, under the REAL validator. A control whose
#      premise is already false measures nothing, which is how "restore as the
#      wrong role" used to pass by failing at connection setup.
#   3. mutate exactly ONE property
#   4. call the REAL ntm_validate_set — never a re-implementation, never an
#      assertion the control constructs for itself
#   5. assert the EXACT expected stage AND the EXACT expected failure class.
#      "It returned non-zero" is the C-2/C-3 defect and is refused here.
#   6. assert that no LATER stage ran, so the rejection happened where it was
#      supposed to and nothing downstream ever saw attacker-chosen bytes
#
# AND THE PART THAT MATTERS MOST
# ------------------------------
# The whole battery is then re-run against four deliberately broken gates:
#
#   bypassed     the validator is deleted / short-circuited — always accepts
#   blind        the real verdict, but the failure class overwritten
#   stageless    the real verdict, but the stage overwritten
#   allstages    the real verdict, but every stage reported as having run
#
# Each must turn the battery RED, and the exact population that must go red is
# asserted. That is the proof that these controls have failing inputs: if
# deleting the validator leaves the suite green, the suite is decoration.
#
# Exit 0 = green. Exit 1 = red.
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/nt-manifest.sh
. "$HERE/../lib/nt-manifest.sh"

W="$(mktemp -d "${TMPDIR:-/tmp}/a5test.XXXXXX")"
chmod 700 "$W"
trap 'rm -rf "$W"' EXIT INT TERM
export NTM_TMP="$W"

OK=0; BAD=0
note(){ printf '  %-6s %-58s %s\n' "$1" "$2" "${3:-}"; }
ok(){   OK=$((OK+1));  note ok "$1" "${2:-}"; }
nok(){  BAD=$((BAD+1)); note NOT-OK "$1" "${2:-}"; }

SETID_A=nt-v2-20260814T031500Z
SETID_B=nt-v2-20260813T031500Z
REMOTE_A=r2:homelab-backups
REMOTE_DR=r2dr:homelab-backups-dr
CREATED_A=2026-08-14T03:15:00Z
READY_A=2026-08-14T03:18:00Z
COMMIT_A=2026-08-14T03:18:20Z
NOW_A=$(date -u -d "$COMMIT_A" +%s); NOW_A=$(( NOW_A + 600 ))

KEY="$W/component-key.txt"
KEY2="$W/other-key.txt"
KEY_EMPTY="$W/empty-key.txt"
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$KEY";  chmod 600 "$KEY"
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$KEY2"; chmod 600 "$KEY2"
: > "$KEY_EMPTY"; chmod 600 "$KEY_EMPTY"

# ── fixture construction ────────────────────────────────────────────────────
# The manifest body is written here, but it is SEALED and the markers are
# WRITTEN by the real producer functions in nt-manifest.sh. The test never
# recomputes a tag or a MAC of its own: if the producers and the validator
# ever disagree, this suite goes red rather than papering over it.
write_manifest_body(){ # <dir> <set_id> <created_utc> <out.json>
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import hashlib, json, os, sys
d, setid, created, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
comps = []
for n, k in (("db.dump.gpg", "db"), ("globals.sql.gpg", "db"),
             ("dbconfig.tar.gz.gpg", "db"), ("rootkey.bin.gpg", "rootkey")):
    b = open(os.path.join(d, n), "rb").read()
    comps.append({
        "name": n, "key": k,
        "plaintext_sha256": hashlib.sha256(b + b"plaintext").hexdigest(),
        "ciphertext_sha256": hashlib.sha256(b).hexdigest(),
        "ciphertext_size": len(b),
        "bound_set_id": setid,
        "set_binding_hmac": "0" * 64,      # replaced by ntm_seal_manifest
    })
tag = hashlib.sha256(setid.encode()).hexdigest()
m = {
  "set_id": setid, "manifest_schema_version": 3, "recovery_set_version": 2,
  "created_utc": created,
  "producer": {"host": "homelab", "tool": "nt-recovery-set.sh", "tool_version": "2"},
  "remotes": ["r2:homelab-backups", "r2dr:homelab-backups-dr"],
  "source": {"container": "natetrader-supabase-db-1", "database": "postgres",
             "dump_role": "supabase_admin", "postgres_version": "17.6",
             "image_ref": "supabase/postgres:17.6.1.136",
             "image_id": "sha256:" + tag,
             "image_repo_digest": "supabase/postgres@sha256:" + tag,
             "extensions": "pgcrypto=1.3,supabase_vault=0.3.1"},
  "fingerprints": {"roles_sha256": hashlib.sha256(b"roles" + setid.encode()).hexdigest(),
                   "catalogue_sha256": hashlib.sha256(b"cat" + setid.encode()).hexdigest(),
                   "catalogue_census": {"database": 1, "schema": 12, "rel": 88,
                                        "role": 31, "pwverifier": 9}},
  "vault_commitment": {"alg": "hmac-sha256",
                       "digest": hashlib.sha256(b"vault" + setid.encode()).hexdigest(),
                       "key_hex": hashlib.sha256(b"vkey" + setid.encode()).hexdigest(),
                       "note": "source-time keyed commitment bound to this set"},
  "archive": {"toc_entries": 842, "sha256": hashlib.sha256(b"arch" + setid.encode()).hexdigest(),
              "size": 1715590, "toc_declares_vault_table_data": True,
              "fully_consumed": True, "globals_create_role_count": 31},
  "snapshot_bound_counts": {"accounts": 3, "equity_snapshots": 118, "audit_log": 42,
                            "auth_users": 2, "vault_secrets": 2, "positions": 7,
                            "trades": 31, "profiles": 2, "vault_refs_ok": True,
                            "accounts_by_mode": {"paper": 3}},
  "root_key": {"path": "/etc/postgresql-custom/pgsodium_root.key", "mode": "600",
               "owner": "105:106", "size": 64,
               "sha256": hashlib.sha256(b"rk" + setid.encode()).hexdigest()},
  "encryption": {"format": "gpg-symmetric-aes256-ocb-aead", "key_separation": True,
                 "db_key_file": "/srv/homelab/secrets/nt-v2-db-key.txt",
                 "rootkey_key_file": "/srv/homelab/secrets/nt-v2-rootkey-key.txt"},
  "components": comps,
  "restore_order": ["1. create fresh data and db-config volumes",
                    "2. install the root key before the first postgres start",
                    "3. apply globals, then pg_restore --exit-on-error"],
}
open(out, "w").write(json.dumps(m))
PY
}

build_set(){ # <dir> <set_id> <created> <ready> <commit> <remote> <keyfile> <n_components>
  local dir="$1" sid="$2" created="$3" ready="$4" commit="$5" remote="$6" kf="$7"
  rm -rf "$dir"; mkdir -p "$dir"
  local n
  for n in db.dump.gpg globals.sql.gpg dbconfig.tar.gz.gpg rootkey.bin.gpg; do
    head -c $(( 4096 + RANDOM )) /dev/urandom > "$dir/$n"
  done
  write_manifest_body "$dir" "$sid" "$created" "$dir/.body.json" || return 1
  ntm_seal_manifest "$dir/.body.json" "$dir/MANIFEST.json" "$kf" || return 1
  rm -f "$dir/.body.json"
  ntm_write_ready  "$dir" "$sid" "$remote" 4 "$ready" || return 1
  ntm_write_commit "$dir" "$remote" "$commit" "$kf"   || return 1
  return 0
}

# ── fixture edit helpers (used only by mutators) ────────────────────────────
medit(){ # medit <dir> <python statements over `d`> ; rewrites canonically
  python3 - "$1/MANIFEST.json" "$2" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
exec(sys.argv[2])
open(p, "w").write(json.dumps(d, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True) + "\n")
PY
}

# The attacker's full public-hash capability: every digest that is a plain,
# unkeyed sha256 gets recomputed to match whatever they just wrote. This is
# precisely the move the legacy chain had no answer to, so the controls that
# need to reach a later stage use it rather than pretending an attacker would
# forget.
refresh_public_hashes(){ # <dir>
  python3 - "$1" <<'PY'
import hashlib, os, sys
d = sys.argv[1]
h = hashlib.sha256(open(os.path.join(d, "MANIFEST.json"), "rb").read()).hexdigest()
p = os.path.join(d, "READY")
out = []
for line in open(p).read().splitlines():
    if line.startswith("manifest_sha256="):
        line = "manifest_sha256=" + h
    out.append(line)
open(p, "w").write("\n".join(out) + "\n")
PY
}

kedit(){ # kedit <file> <python statements over `lines` (list of str)>
  python3 - "$1" "$2" <<'PY'
import sys
p = sys.argv[1]
lines = open(p).read().splitlines()
exec(sys.argv[2])
open(p, "w").write("\n".join(lines) + "\n")
PY
}

# ── the master fixtures ─────────────────────────────────────────────────────
GOOD="$W/good/$SETID_A"
OTHER="$W/other/$SETID_B"
build_set "$GOOD"  "$SETID_A" "$CREATED_A" "$READY_A" "$COMMIT_A" "$REMOTE_A" "$KEY" \
  || { echo "FATAL: could not build the good fixture"; exit 1; }
build_set "$OTHER" "$SETID_B" 2026-08-13T03:15:00Z 2026-08-13T03:18:00Z \
          2026-08-13T03:18:20Z "$REMOTE_A" "$KEY" \
  || { echo "FATAL: could not build the second genuine fixture"; exit 1; }

echo "A-5 manifest/marker validator   python=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "fixtures: $(basename "$GOOD") and $(basename "$OTHER") (a second GENUINE set, same producer key)"
echo

# ── stage-order arithmetic ──────────────────────────────────────────────────
# Fork-free on purpose: this runs 51 times per battery across five batteries,
# and a command substitution per stage comparison costs more wall clock than
# every assertion in the suite put together.
declare -A STAGE_POS
_spi=0
for _s in "${NTM_STAGE_ORDER[@]}"; do STAGE_POS["$_s"]=$_spi; _spi=$((_spi+1)); done

# Every stage strictly after <stage> must be absent from NTM_STAGES_RUN.
# An unknown stage name maps to -1, so EVERY stage counts as "later" and the
# control fails loudly rather than silently skipping the check.
LATER_RAN=''
later_stages_that_ran(){ # <stage> -> sets LATER_RAN
  local want="$1" wi="${STAGE_POS[$1]:--1}" s out=''
  for s in "${NTM_STAGE_ORDER[@]}"; do
    [[ "${STAGE_POS[$s]}" -le "$wi" ]] && continue
    ntm_stage_ran "$s" && out="$out $s"
  done
  LATER_RAN="${out# }"
}

# ── the gates ───────────────────────────────────────────────────────────────
# gate_real is the only one that is allowed to be correct. The other four are
# the falsification harness: each disables exactly one dimension of the
# assertion, and the battery must go red under every one of them.
gate_real(){ ntm_validate_set "$@"; }

gate_bypassed(){                      # the validator deleted / short-circuited
  NTM_ERR=''; NTM_ERR_DETAIL=''; NTM_STAGE=done; NTM_LEVEL=full
  NTM_STAGES_RUN="${NTM_STAGE_ORDER[*]}"; return 0; }

gate_blind(){                         # real verdict, class thrown away
  local rc=0; ntm_validate_set "$@" || rc=$?; NTM_ERR=rejected; return $rc; }

gate_stageless(){                     # real verdict, stage thrown away
  local rc=0; ntm_validate_set "$@" || rc=$?; NTM_STAGE=unknown; return $rc; }

gate_allstages(){                     # real verdict, "everything ran" claimed
  local rc=0; ntm_validate_set "$@" || rc=$?
  NTM_STAGES_RUN="${NTM_STAGE_ORDER[*]}"; return $rc; }

# ── control registry ────────────────────────────────────────────────────────
CTL_LABEL=(); CTL_STAGE=(); CTL_CLASS=(); CTL_FN=()
neg(){ CTL_LABEL+=("$1"); CTL_STAGE+=("$2"); CTL_CLASS+=("$3"); CTL_FN+=("$4"); }
pos(){ CTL_LABEL+=("$1"); CTL_STAGE+=(done);  CTL_CLASS+=('');  CTL_FN+=("$2"); }

CASE=''; CASE_SETID=''; CASE_REMOTE=''; CASE_KEY=''; CASE_DEPTH=''
CASE_NOW=''; CASE_SCHEMA=''; CASE_TOOLS=''; CASE_PRODUCER=''; CASE_EXTRA=''

run_gate(){ # run_gate <gate> ; uses the CASE_* settings
  NTM_ERR='__unset__'; NTM_STAGE='__unset__'; NTM_STAGES_RUN='__unset__'; NTM_LEVEL='__unset__'
  local rc=0
  NTM_NOW="$CASE_NOW" \
  NTM_SCHEMA_VERSIONS="$CASE_SCHEMA" \
  NTM_TOOL_VERSIONS="$CASE_TOOLS" \
  NTM_EXPECT_PRODUCER="$CASE_PRODUCER" \
    "$1" "$CASE" "$CASE_SETID" "$CASE_REMOTE" "$CASE_KEY" "$CASE_DEPTH" || rc=$?
  return $rc
}

# ── mutators ────────────────────────────────────────────────────────────────
# Each changes exactly one property of the pristine copy in $CASE, or exactly
# one argument the validator is called with.

# layout
m_dir_missing(){    CASE="$W/there-is-no-such-set"; }
m_setid_traversal(){ CASE_SETID='../../etc'; }
m_setid_shape(){    CASE_SETID='not-a-set-id'; }
m_bad_depth(){      CASE_DEPTH=thorough; }
m_key_empty(){      CASE_KEY="$KEY_EMPTY"; }

# manifest
m_manifest_gone(){   rm -f "$CASE/MANIFEST.json"; }
m_manifest_empty(){  : > "$CASE/MANIFEST.json"; }
m_manifest_garbage(){ printf '{ this is not json\n' > "$CASE/MANIFEST.json"; }
m_manifest_dup(){
  python3 - "$CASE/MANIFEST.json" <<'PY'
import sys
p = sys.argv[1]; t = open(p).read()
open(p, "w").write('{"created_utc":"2026-08-14T03:15:00Z","created_utc":"2026-08-14T03:15:00Z",' + t[1:])
PY
}
m_manifest_pretty(){
  python3 - "$CASE/MANIFEST.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))          # read fully BEFORE opening for write: python
open(p, "w").write(json.dumps(d, indent=2) + "\n")   # evaluates args left to right
PY
}
m_field_missing(){ medit "$CASE" 'del d["fingerprints"]'; }
m_field_unknown(){ medit "$CASE" 'd["harmless_extra"]="hello"'; }
m_field_empty(){   medit "$CASE" 'd["source"]["container"]=""'; }
m_field_type(){    medit "$CASE" 'd["archive"]["size"]="1715590"'; }
m_field_malformed(){ medit "$CASE" 'd["fingerprints"]["catalogue_sha256"]="z"*64'; }
m_field_truebool(){ medit "$CASE" 'd["archive"]["fully_consumed"]=False'; }
m_manifest_setid(){ medit "$CASE" 'd["set_id"]="not a set id"'; }
m_comp_traversal(){ medit "$CASE" 'd["components"][0]["name"]="../../etc/passwd"'; }

# markers
m_ready_gone(){   rm -f "$CASE/READY"; }
m_commit_gone(){  rm -f "$CASE/COMMIT"; }
m_ready_junk(){   kedit "$CASE/READY"  'lines[2]="!! not a key=value line !!"'; }
m_commit_nomac(){ kedit "$CASE/COMMIT" 'lines=[l for l in lines if not l.startswith("mac=")]'; }
m_commit_dup(){   kedit "$CASE/COMMIT" 'lines.append([l for l in lines if l.startswith("set_id=")][0])'; }

# envelope
m_binding_garbage(){
  kedit "$CASE/COMMIT" 'import base64
lines=[("binding_b64="+base64.b64encode(b"this is not a binding block").decode()) if l.startswith("binding_b64=") else l for l in lines]'
}
m_wrong_key(){ CASE_KEY="$KEY2"; }
# The headline A-5 scenario: the attacker rewrites MANIFEST.json, recomputes
# every PLAIN hash into the markers — including the binding block's own
# manifest_sha256 — and cannot recompute the keyed MAC.
m_attacker_recomputes_hashes(){
  medit "$CASE" 'd["fingerprints"]["catalogue_sha256"]="a"*64'
  refresh_public_hashes "$CASE"
  python3 - "$CASE" <<'PY'
import base64, hashlib, json, os, sys
d = sys.argv[1]
msha = hashlib.sha256(open(os.path.join(d, "MANIFEST.json"), "rb").read()).hexdigest()
p = os.path.join(d, "COMMIT")
out = []
for line in open(p).read().splitlines():
    if line.startswith("binding_b64="):
        b = json.loads(base64.b64decode(line.split("=", 1)[1]).decode())
        b["manifest_sha256"] = msha
        b["catalogue_sha256"] = "a" * 64
        blob = (json.dumps(b, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True) + "\n").encode()
        line = "binding_b64=" + base64.b64encode(blob).decode()
    out.append(line)          # the mac= line is left exactly as it was
open(p, "w").write("\n".join(out) + "\n")
PY
}
m_replay_marker(){ cp "$OTHER/COMMIT" "$CASE/COMMIT"; }

# identity
m_setid_swap(){    medit "$CASE" 'd["set_id"]="nt-v2-20250101T000000Z"'; }
m_schema_ver(){    medit "$CASE" 'd["manifest_schema_version"]=4'; }
m_tool_ver(){      CASE_TOOLS='2'; medit "$CASE" 'd["producer"]["tool_version"]="99"'; }
m_producer(){      CASE_PRODUCER='homelab|nt-recovery-set.sh|2'
                   medit "$CASE" 'd["producer"]["host"]="impostor"'; }
m_ready_other_remote(){ CASE_REMOTE="$REMOTE_DR"; }   # markers were issued for r2
m_commit_header_setid(){ kedit "$CASE/COMMIT" 'lines=[("set_id=nt-v2-20250101T000000Z" if l.startswith("set_id=") else l) for l in lines]'; }

# ready
m_ready_before_manifest(){
  ntm_write_ready  "$CASE" "$SETID_A" "$REMOTE_A" 4 2026-08-14T03:00:00Z
  ntm_write_commit "$CASE" "$REMOTE_A" 2026-08-14T03:00:20Z "$KEY"
  CASE_NOW=$(( $(date -u -d 2026-08-14T03:00:20Z +%s) + 600 ))
}
m_ready_undercount(){
  ntm_write_ready  "$CASE" "$SETID_A" "$REMOTE_A" 3 "$READY_A"
  ntm_write_commit "$CASE" "$REMOTE_A" "$COMMIT_A" "$KEY"
}
m_ready_old(){ CASE_NOW=$(( NOW_A + 10 * 86400 )); }
m_commit_wrong_ready(){ kedit "$CASE/READY" 'lines=[("ready_utc=2026-08-14T03:18:10Z" if l.startswith("ready_utc=") else l) for l in lines]'; }

# inventory
m_stowaway(){ head -c 512 /dev/urandom > "$CASE/stowaway.gpg"; }
m_dup_component(){
  medit "$CASE" 'd["components"].append(dict(d["components"][0]))'
  ntm_seal_manifest "$CASE/MANIFEST.json" "$CASE/MANIFEST.json.new" "$KEY"
  mv "$CASE/MANIFEST.json.new" "$CASE/MANIFEST.json"
  ntm_write_ready  "$CASE" "$SETID_A" "$REMOTE_A" 5 "$READY_A"
  ntm_write_commit "$CASE" "$REMOTE_A" "$COMMIT_A" "$KEY"
}

# components
m_comp_gone(){      rm -f "$CASE/globals.sql.gpg"; }
m_comp_zero(){      : > "$CASE/globals.sql.gpg"; }
m_comp_truncated(){ local s; s="$(stat -c %s "$CASE/db.dump.gpg")"
                    head -c $(( s / 2 )) "$CASE/db.dump.gpg" > "$CASE/.t" && mv "$CASE/.t" "$CASE/db.dump.gpg"; }
m_comp_longer(){    head -c 64 /dev/urandom >> "$CASE/db.dump.gpg"; }
m_comp_flipped(){   local s; s="$(stat -c %s "$CASE/db.dump.gpg")"
                    printf '\xff' | dd of="$CASE/db.dump.gpg" bs=1 seek=$(( s / 2 )) conv=notrunc status=none; }
m_comp_tag_forged(){
  medit "$CASE" 'c=d["components"][0]; h=c["set_binding_hmac"]; c["set_binding_hmac"]=("b" if h[0]!="b" else "c")+h[1:]'
  refresh_public_hashes "$CASE"
}
# A genuine component of another genuine set, together with its genuine
# manifest entry. Size, digest and per-set tag all authenticate — under the
# OTHER set's derived key. Nothing here is forged; it is simply not ours.
m_replay_component(){
  cp "$OTHER/db.dump.gpg" "$CASE/db.dump.gpg"
  python3 - "$CASE/MANIFEST.json" "$OTHER/MANIFEST.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
entry = [c for c in b["components"] if c["name"] == "db.dump.gpg"][0]
a["components"] = [entry if c["name"] == "db.dump.gpg" else c for c in a["components"]]
open(sys.argv[1], "w").write(json.dumps(a, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True) + "\n")
PY
  refresh_public_hashes "$CASE"
}

# bindings — the manifest is rewritten and every public hash recomputed; only
# the keyed commitment still disagrees, and it names the field.
m_binding_snapshot(){ medit "$CASE" 'd["snapshot_bound_counts"]["accounts"]=999'; refresh_public_hashes "$CASE"; }
m_binding_catalogue(){ medit "$CASE" 'd["fingerprints"]["catalogue_sha256"]="4"*64'; refresh_public_hashes "$CASE"; }
m_binding_commitment(){ medit "$CASE" 'd["vault_commitment"]["digest"]="5"*64'; refresh_public_hashes "$CASE"; }
m_binding_manifest(){ medit "$CASE" 'd["restore_order"][0]="1. actually, do something else entirely"'
                      refresh_public_hashes "$CASE"; }
# The manifest is re-sealed around a substituted component (so the components
# stage is satisfied) but COMMIT is still the one from the original run.
m_binding_inventory(){
  head -c 9999 /dev/urandom > "$CASE/db.dump.gpg"
  python3 - "$CASE/MANIFEST.json" "$CASE/db.dump.gpg" <<'PY'
import hashlib, json, sys
p = sys.argv[1]; b = open(sys.argv[2], "rb").read()
d = json.load(open(p))
for c in d["components"]:
    if c["name"] == "db.dump.gpg":
        c["ciphertext_size"] = len(b)
        c["ciphertext_sha256"] = hashlib.sha256(b).hexdigest()
open(p, "w").write(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
PY
  ntm_seal_manifest "$CASE/MANIFEST.json" "$CASE/MANIFEST.json.new" "$KEY"
  mv "$CASE/MANIFEST.json.new" "$CASE/MANIFEST.json"
  refresh_public_hashes "$CASE"
}

# positives
m_none(){ :; }
m_depth_markers(){ CASE_DEPTH=markers; }
m_dr_remote(){
  CASE_REMOTE="$REMOTE_DR"
  ntm_write_ready  "$CASE" "$SETID_A" "$REMOTE_DR" 4 "$READY_A"
  ntm_write_commit "$CASE" "$REMOTE_DR" "$COMMIT_A" "$KEY"
}
m_second_set(){
  CASE="$W/case-other"; rm -rf "$CASE"; cp -a "$OTHER" "$CASE"
  CASE_SETID="$SETID_B"
  CASE_NOW=$(( $(date -u -d 2026-08-13T03:18:20Z +%s) + 600 ))
}

# ── registry ────────────────────────────────────────────────────────────────
pos "POSITIVE: pristine set validates end to end"           m_none
pos "POSITIVE: pristine set validates at depth=markers"     m_depth_markers
pos "POSITIVE: a second genuine set validates on its terms" m_second_set
pos "POSITIVE: same set, markers issued for the DR remote"  m_dr_remote

neg "absent set directory"                    layout     set_dir_missing            m_dir_missing
neg "set id containing a path traversal"      layout     unsafe_path                m_setid_traversal
neg "set id of the wrong shape"               layout     set_id_malformed           m_setid_shape
neg "depth neither full nor markers"          layout     usage_bad_depth            m_bad_depth
neg "empty component key file"                layout     keyfile_unusable           m_key_empty

neg "MANIFEST.json absent"                    manifest   manifest_missing           m_manifest_gone
neg "MANIFEST.json zero length"               manifest   manifest_empty             m_manifest_empty
neg "MANIFEST.json not JSON"                  manifest   manifest_not_json          m_manifest_garbage
neg "duplicate field in MANIFEST.json"        manifest   manifest_duplicate_field   m_manifest_dup
neg "non-canonical JSON (pretty printed)"     manifest   manifest_non_canonical     m_manifest_pretty
neg "missing required field"                  manifest   field_missing              m_field_missing
neg "unknown field under a closed schema"     manifest   field_unknown              m_field_unknown
neg "empty required field"                    manifest   field_empty                m_field_empty
neg "field of the wrong type"                 manifest   field_type                 m_field_type
neg "malformed field (digest is not hex)"     manifest   field_malformed            m_field_malformed
neg "asserted-true field set false"           manifest   field_malformed            m_field_truebool
neg "invalid set id inside the manifest"      manifest   set_id_malformed           m_manifest_setid
neg "component name with a path traversal"    manifest   unsafe_path                m_comp_traversal

neg "READY absent"                            markers    ready_missing              m_ready_gone
neg "COMMIT absent"                           markers    commit_missing             m_commit_gone
neg "READY with a malformed line"             markers    ready_malformed            m_ready_junk
neg "COMMIT with no mac line"                 markers    commit_malformed           m_commit_nomac
neg "COMMIT with a duplicated field"          markers    commit_duplicate_field     m_commit_dup

neg "binding block is not a binding block"    envelope   envelope_binding_unreadable m_binding_garbage
neg "wrong component key"                     envelope   envelope_mac_mismatch      m_wrong_key
neg "attacker rewrites manifest + all hashes" envelope   envelope_mac_mismatch      m_attacker_recomputes_hashes
neg "COMMIT lifted from another genuine set"  envelope   replay_marker_foreign_set  m_replay_marker

neg "manifest names a different set"          identity   set_id_mismatch            m_setid_swap
neg "unsupported manifest schema version"     identity   schema_version_unsupported m_schema_ver
neg "unsupported producer tool version"       identity   tool_version_unsupported   m_tool_ver
neg "wrong producer identity"                 identity   producer_identity_mismatch m_producer
neg "READY belongs to the other remote"       identity   remote_identity_mismatch   m_ready_other_remote
neg "COMMIT header disagrees with its binding" identity  commit_inconsistent        m_commit_header_setid

neg "READY predates the manifest it attests"  ready      ready_premature            m_ready_before_manifest
neg "READY attests fewer components than sent" ready     ready_premature            m_ready_undercount
neg "READY is stale"                          ready      ready_stale                m_ready_old
neg "COMMIT references a different READY"     ready      commit_inconsistent        m_commit_wrong_ready

neg "unlisted object on the set surface"      inventory  inventory_mismatch         m_stowaway
neg "component listed twice"                  inventory  inventory_duplicate        m_dup_component

neg "component object deleted"                components component_missing          m_comp_gone
neg "component truncated to zero length"      components component_empty            m_comp_zero
neg "component truncated to half length"      components component_truncated        m_comp_truncated
neg "component longer than recorded"          components component_size_mismatch    m_comp_longer
neg "ciphertext digest mismatch (same size)"  components component_digest_mismatch  m_comp_flipped
neg "forged per-set component tag"            components component_binding_invalid  m_comp_tag_forged
neg "component lifted from another genuine set" components replay_component_foreign_set m_replay_component

neg "wrong source snapshot"                   bindings   binding_source_snapshot_mismatch   m_binding_snapshot
neg "wrong catalogue digest"                  bindings   binding_catalogue_digest_mismatch  m_binding_catalogue
neg "source-time commitment mismatch"         bindings   binding_source_commitment_mismatch m_binding_commitment
neg "inventory disagrees with the commitment" bindings   binding_inventory_mismatch         m_binding_inventory
neg "non-binding field rewritten (catch-all)" bindings   binding_manifest_digest_mismatch   m_binding_manifest

N_CTL=${#CTL_LABEL[@]}
N_POS=0; N_NEG=0
for i in $(seq 0 $((N_CTL-1))); do
  if [[ -z "${CTL_CLASS[$i]}" ]]; then N_POS=$((N_POS+1)); else N_NEG=$((N_NEG+1)); fi
done

# ── the battery ─────────────────────────────────────────────────────────────
BAT_RED_POS=0; BAT_RED_NEG=0; BAT_PREMISE_FAIL=0; BAT_INTERNAL=0
run_battery(){ # run_battery <gate> <verbose:yes|no>
  local gate="$1" verbose="$2" i label want_stage want_class fn rc
  BAT_RED_POS=0; BAT_RED_NEG=0; BAT_PREMISE_FAIL=0; BAT_INTERNAL=0
  for i in $(seq 0 $((N_CTL-1))); do
    label="${CTL_LABEL[$i]}"; want_stage="${CTL_STAGE[$i]}"
    want_class="${CTL_CLASS[$i]}"; fn="${CTL_FN[$i]}"

    CASE="$W/case"; rm -rf "$CASE"; cp -a "$GOOD" "$CASE"
    CASE_SETID="$SETID_A"; CASE_REMOTE="$REMOTE_A"; CASE_KEY="$KEY"
    CASE_DEPTH=full; CASE_NOW="$NOW_A"; CASE_SCHEMA=''; CASE_TOOLS=''; CASE_PRODUCER=''

    # (2) unrelated prerequisites: the untouched copy must validate cleanly
    # through EVERY stage under the REAL validator. Always the real one, even
    # when the gate under test is a broken stub — otherwise a broken stub could
    # hide a broken premise.
    if ! run_gate gate_real; then
      BAT_PREMISE_FAIL=$((BAT_PREMISE_FAIL+1))
      [[ "$verbose" == yes ]] && nok "$label" "PREMISE FALSE: pristine copy rejected ($NTM_STAGE/$NTM_ERR)"
      continue
    fi

    # (3) exactly one property
    "$fn"

    # (4) the gate under test
    rc=0; run_gate "$gate" || rc=$?

    if [[ "$NTM_ERR" == validator_internal_error ]]; then
      BAT_INTERNAL=$((BAT_INTERNAL+1))
      [[ "$verbose" == yes ]] && nok "$label" "validator crashed: $NTM_ERR_DETAIL"
      [[ -z "$want_class" ]] && BAT_RED_POS=$((BAT_RED_POS+1)) || BAT_RED_NEG=$((BAT_RED_NEG+1))
      continue
    fi

    if [[ -z "$want_class" ]]; then
      # positive control
      local why=''
      [[ $rc -eq 0 ]]                 || why="rejected ($NTM_STAGE/$NTM_ERR: $NTM_ERR_DETAIL)"
      [[ -z "$why" && -n "$NTM_ERR" ]] && why="accepted but NTM_ERR is '$NTM_ERR'"
      [[ -z "$why" && "$NTM_STAGE" != done ]] && why="accepted but stage is '$NTM_STAGE'"
      if [[ -z "$why" && "$CASE_DEPTH" == markers ]]; then
        [[ "$NTM_LEVEL" == markers ]] || why="depth=markers but NTM_LEVEL='$NTM_LEVEL'"
        [[ -z "$why" ]] && ntm_stage_ran components && why="depth=markers still ran the components stage"
        [[ -z "$why" ]] && ntm_stage_ran inventory  && why="depth=markers still ran the inventory stage"
      fi
      if [[ -z "$why" && "$CASE_DEPTH" == full ]]; then
        ntm_stage_ran components || why="depth=full did not run the components stage"
        [[ -z "$why" ]] && { ntm_stage_ran bindings || why="depth=full did not run the bindings stage"; }
      fi
      if [[ -z "$why" ]]; then
        [[ "$verbose" == yes ]] && ok "$label" "stages=$(wc -w <<<"$NTM_STAGES_RUN") level=$NTM_LEVEL"
      else
        BAT_RED_POS=$((BAT_RED_POS+1))
        [[ "$verbose" == yes ]] && nok "$label" "$why"
      fi
      continue
    fi

    # negative control: (5) exact stage AND class, (6) no later stage ran
    local why=''
    [[ $rc -ne 0 ]] || why="ACCEPTED the mutated set"
    [[ -z "$why" && "$NTM_ERR" != "$want_class" ]] \
      && why="wrong class: got '$NTM_ERR', want '$want_class'"
    [[ -z "$why" && "$NTM_STAGE" != "$want_stage" ]] \
      && why="wrong stage: got '$NTM_STAGE', want '$want_stage'"
    if [[ -z "$why" ]]; then
      later_stages_that_ran "$want_stage"
      [[ -n "$LATER_RAN" ]] && why="stages after $want_stage still ran: $LATER_RAN"
    fi
    if [[ -z "$why" ]]; then
      [[ "$verbose" == yes ]] && ok "$label" "$want_stage/$want_class"
    else
      BAT_RED_NEG=$((BAT_RED_NEG+1))
      [[ "$verbose" == yes ]] && nok "$label" "$why"
    fi
  done
  return 0
}

echo "-- the battery against the REAL validator ($N_POS positive, $N_NEG negative controls) --"
run_battery gate_real yes
echo
if [[ $BAT_PREMISE_FAIL -ne 0 ]]; then
  nok "every control's premise holds" "$BAT_PREMISE_FAIL control(s) started from a rejected fixture"
else
  ok "every control's premise holds" "$N_CTL pristine validations"
fi
if [[ $BAT_INTERNAL -ne 0 ]]; then
  nok "the validator never reports validator_internal_error" "$BAT_INTERNAL occurrence(s)"
else
  ok "the validator never reports validator_internal_error"
fi

# ── every enumerated class is actually exercised ────────────────────────────
# A class nobody can produce is a class nobody tests. This is the guard against
# quietly dropping a control while leaving its class in the documentation.
REQUIRED_CLASSES=(
  set_dir_missing unsafe_path set_id_malformed usage_bad_depth keyfile_unusable
  manifest_missing manifest_empty manifest_not_json manifest_duplicate_field
  manifest_non_canonical field_missing field_unknown field_empty field_type
  field_malformed ready_missing commit_missing ready_malformed commit_malformed
  commit_duplicate_field envelope_binding_unreadable envelope_mac_mismatch
  replay_marker_foreign_set set_id_mismatch schema_version_unsupported
  tool_version_unsupported producer_identity_mismatch remote_identity_mismatch
  commit_inconsistent ready_premature ready_stale inventory_mismatch
  inventory_duplicate component_missing component_empty component_truncated
  component_size_mismatch component_digest_mismatch component_binding_invalid
  replay_component_foreign_set binding_source_snapshot_mismatch
  binding_catalogue_digest_mismatch binding_source_commitment_mismatch
  binding_inventory_mismatch binding_manifest_digest_mismatch
)
missing_classes=''
for c in "${REQUIRED_CLASSES[@]}"; do
  hit=no
  for k in "${CTL_CLASS[@]}"; do [[ "$k" == "$c" ]] && { hit=yes; break; }; done
  [[ "$hit" == yes ]] || missing_classes="$missing_classes $c"
done
if [[ -z "$missing_classes" ]]; then
  ok "every enumerated failure class has a control" "${#REQUIRED_CLASSES[@]} classes"
else
  nok "every enumerated failure class has a control" "no control for:$missing_classes"
fi

# ── the falsification harness ───────────────────────────────────────────────
# Each broken gate disables exactly one dimension of the assertion. If the
# battery survives any of them, that dimension is decoration.
echo
echo "-- falsification: the battery must go RED when the validator is broken --"
meta(){ # meta <label> <gate> <expect_red_neg> <expect_red_pos|any>
  local label="$1" gate="$2" want_neg="$3" want_pos="$4" why=''
  run_battery "$gate" no
  [[ $BAT_PREMISE_FAIL -eq 0 ]] || why="premises broke under the stub ($BAT_PREMISE_FAIL)"
  [[ -z "$why" && "$BAT_RED_NEG" -ne "$want_neg" ]] \
    && why="negatives red: $BAT_RED_NEG, expected $want_neg"
  [[ -z "$why" && "$want_pos" != any && "$BAT_RED_POS" -ne "$want_pos" ]] \
    && why="positives red: $BAT_RED_POS, expected $want_pos"
  if [[ -z "$why" ]]; then ok "$label" "red: $BAT_RED_NEG/$N_NEG neg, $BAT_RED_POS/$N_POS pos"
  else nok "$label" "$why"; fi
}
# The claim under test in each row is about the NEGATIVE controls: every one of
# them must notice. Positives are left unconstrained for the two stubs that
# fabricate NTM_STAGES_RUN/NTM_LEVEL, because a stub claiming "full depth, all
# stages ran" also trips the depth=markers positive — a true observation, but
# not the claim being made here, and pinning it to a literal count would break
# the moment another depth=markers positive is added.
meta "deleting/bypassing the validator turns the battery red" gate_bypassed  "$N_NEG" any
meta "discarding the failure class turns the battery red"     gate_blind     "$N_NEG" "$N_POS"
meta "discarding the stage turns the battery red"             gate_stageless "$N_NEG" "$N_POS"
meta "claiming every stage ran turns the battery red"         gate_allstages "$N_NEG" any

# ── the specific claim the audit made ───────────────────────────────────────
# Two separate defects are demonstrated here, both by measurement.
#
# (i) THE CHAIN. The legacy trust chain is "sha256(MANIFEST.json) equals the
#     manifest_sha256 recorded in the marker". Both files live in the same
#     bucket prefix, so an attacker with write access owns both sides of the
#     comparison. Shown below on a real fixture: it accepts a wholesale
#     rewrite as soon as the recorded digest is recomputed — which costs the
#     attacker one sha256sum.
#
# (ii) THE CONTROL. The legacy tamper CONTROL never substituted its variant
#      into any gate. Shown below by deleting the gate outright and observing
#      that the control's verdict does not move.
echo
echo "-- (i) the legacy hash chain, on inputs where it and the real validator disagree --"

# the legacy chain, reproduced in behaviour
legacy_chain(){ # <dir> -> ACCEPT | REJECT
  local s; s="$(sha256sum "$1/MANIFEST.json" | cut -d' ' -f1)"
  if grep -q "^manifest_sha256=$s$" "$1/READY"; then echo ACCEPT; else echo REJECT; fi
}
real_verdict(){ # <dir> <extra mutator already applied> -> sets RV_RC/RV_STAGE/RV_CLASS
  CASE="$1"; CASE_SETID="$SETID_A"; CASE_REMOTE="$REMOTE_A"; CASE_KEY="$KEY"
  CASE_DEPTH=full; CASE_NOW="$NOW_A"; CASE_SCHEMA=''; CASE_TOOLS=''; CASE_PRODUCER=''
  RV_RC=0; run_gate gate_real || RV_RC=$?
  RV_STAGE="$NTM_STAGE"; RV_CLASS="$NTM_ERR"
}

# baseline: on an untouched set both must ACCEPT, or the comparison below is
# not comparing anything
L0="$W/leg-pristine"; rm -rf "$L0"; cp -a "$GOOD" "$L0"
LEG0="$(legacy_chain "$L0")"; real_verdict "$L0"; R0="$RV_RC"
note INFO "pristine set" "legacy=$LEG0  real=$([[ $R0 -eq 0 ]] && echo ACCEPT || echo "REJECT($RV_CLASS)")"

# attacker A: rewrites the manifest and forgets the marker. Both catch it —
# this is the only case the legacy chain was ever able to catch.
L1="$W/leg-naive"; rm -rf "$L1"; cp -a "$GOOD" "$L1"
CASE="$L1"; medit "$CASE" 'd["fingerprints"]["catalogue_sha256"]="7"*64'
LEG1="$(legacy_chain "$L1")"; real_verdict "$L1"; R1="$RV_RC"; C1="$RV_CLASS"
note INFO "manifest rewritten, marker untouched" "legacy=$LEG1  real=$([[ $R1 -eq 0 ]] && echo ACCEPT || echo "REJECT($C1)")"

# attacker B: rewrites the manifest AND recomputes the plain digest into the
# marker. One extra sha256sum. This is the hole.
L2="$W/leg-recomputed"; rm -rf "$L2"; cp -a "$GOOD" "$L2"
CASE="$L2"; medit "$CASE" 'd["fingerprints"]["catalogue_sha256"]="7"*64'
refresh_public_hashes "$L2"
LEG2="$(legacy_chain "$L2")"; real_verdict "$L2"; R2="$RV_RC"; C2="$RV_CLASS"; S2="$RV_STAGE"
note INFO "manifest rewritten + marker digest recomputed" "legacy=$LEG2  real=$([[ $R2 -eq 0 ]] && echo ACCEPT || echo "REJECT($C2)")"

# attacker C: as B, and additionally rewrites the binding block to be
# self-consistent. Cannot recompute the MAC, so it lands one stage earlier.
L3="$W/leg-selfconsistent"; rm -rf "$L3"; cp -a "$GOOD" "$L3"
CASE="$L3"; m_attacker_recomputes_hashes
LEG3="$(legacy_chain "$L3")"; real_verdict "$L3"; R3="$RV_RC"; C3="$RV_CLASS"; S3="$RV_STAGE"
note INFO "...and the binding block made self-consistent" "legacy=$LEG3  real=$([[ $R3 -eq 0 ]] && echo ACCEPT || echo "REJECT($C3)")"

[[ "$LEG0" == ACCEPT && $R0 -eq 0 ]] \
  && ok "both gates accept the pristine set (the comparison has a baseline)" \
  || nok "both gates accept the pristine set" "legacy=$LEG0 real_rc=$R0 ($RV_CLASS)"
[[ "$LEG1" == REJECT && $R1 -ne 0 ]] \
  && ok "both gates catch a naive manifest rewrite" "real: $C1" \
  || nok "both gates catch a naive manifest rewrite" "legacy=$LEG1 real_rc=$R1"
if [[ "$LEG2" == ACCEPT && $R2 -ne 0 && "$C2" == binding_catalogue_digest_mismatch ]]; then
  ok "legacy ACCEPTS a rewrite once the plain digest is recomputed; the envelope does not" \
     "real: $S2/$C2"
else
  nok "legacy ACCEPTS a rewrite once the plain digest is recomputed" \
      "legacy=$LEG2 real_rc=$R2 $S2/$C2"
fi
if [[ "$LEG3" == ACCEPT && $R3 -ne 0 && "$C3" == envelope_mac_mismatch ]]; then
  ok "a self-consistent forgery dies on the keyed MAC, which the attacker cannot recompute" \
     "real: $S3/$C3"
else
  nok "a self-consistent forgery dies on the keyed MAC" "legacy=$LEG3 real_rc=$R3 $S3/$C3"
fi

echo
echo "-- (ii) the legacy CONTROL's verdict did not depend on the gate existing --"
# Verbatim in behaviour: write a variant of the manifest, hash it, assert the
# hash differs from the recorded one. The gate is represented by a function the
# control is supposed to be exercising.
real_manifest_gate(){ legacy_chain "$1"; }
legacy_tamper_control(){ # <dir> -> PASS | FAIL
  python3 - "$1/MANIFEST.json" "$W/tampered.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["fingerprints"]["catalogue_sha256"] = "0" * 64
json.dump(d, open(sys.argv[2], "w"))
PY
  local recorded tampered
  recorded="$(sha256sum "$1/MANIFEST.json" | cut -d' ' -f1)"
  tampered="$(sha256sum "$W/tampered.json" | cut -d' ' -f1)"
  if [[ "$tampered" != "$recorded" ]]; then echo PASS; else echo FAIL; fi
}
WITH_GATE="$(legacy_tamper_control "$L0")"
unset -f real_manifest_gate
WITHOUT_GATE="$(legacy_tamper_control "$L0")"
note INFO "legacy tamper control, gate present" "$WITH_GATE"
note INFO "legacy tamper control, gate DELETED" "$WITHOUT_GATE"
if [[ "$WITH_GATE" == PASS && "$WITHOUT_GATE" == PASS ]]; then
  ok "the legacy control passes identically with the gate deleted" \
     "it measured its own scratch write, never the artefact"
else
  nok "the legacy control passes identically with the gate deleted" \
      "with=$WITH_GATE without=$WITHOUT_GATE"
fi
# and the contrast: this suite's own controls do NOT survive that treatment —
# that is exactly what the falsification harness above measured.

# ── the producers and the validator must agree ──────────────────────────────
echo
echo "-- the builder-side producers are the ones the validator re-derives from --"
FRESH="$W/fresh/nt-v2-20260812T091500Z"
if build_set "$FRESH" nt-v2-20260812T091500Z 2026-08-12T09:15:00Z \
     2026-08-12T09:18:00Z 2026-08-12T09:18:20Z "$REMOTE_A" "$KEY"; then
  CASE="$FRESH"; CASE_SETID=nt-v2-20260812T091500Z; CASE_REMOTE="$REMOTE_A"
  CASE_KEY="$KEY"; CASE_DEPTH=full
  CASE_NOW=$(( $(date -u -d 2026-08-12T09:18:20Z +%s) + 600 ))
  CASE_SCHEMA=''; CASE_TOOLS=''; CASE_PRODUCER=''
  if run_gate gate_real; then
    ok "a set built only by ntm_seal_manifest/ntm_write_ready/ntm_write_commit validates"
  else
    nok "a set built only by the producers validates" "$NTM_STAGE/$NTM_ERR: $NTM_ERR_DETAIL"
  fi
else
  nok "a set built only by the producers validates" "the producers failed: $NTM_ERR"
fi

echo
echo "A-5: $OK ok, $BAD not-ok"
if [[ $BAD -eq 0 ]]; then echo "A-5 SUITE GREEN"; exit 0
else echo "A-5 SUITE RED"; exit 1; fi
