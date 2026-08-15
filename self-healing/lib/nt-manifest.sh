# shellcheck shell=bash
# ============================================================================
# nt-manifest.sh — THE manifest/marker validator for a V2+ recovery set.
#
# WHY THIS EXISTS (audit A-5, and the C-3 defect it inherited)
# ------------------------------------------------------------
# There was no single validator. The builder checked some things on the way
# out, the drill checked overlapping-but-different things on the way in, and
# scheduled verification checked a third set. Whatever one of them forgot, the
# others did not cover, because none of them was the authority.
#
# The specific control that failed was "manifest tamper detection". It read:
#
#     python3 - MANIFEST.json tampered.json <<'PY' … PY      # write a variant
#     s="$(sha256sum tampered.json | cut -d' ' -f1)"
#     [[ "$s" != "$RECORDED_DIGEST" ]] && echo PASS
#
# It computed the sha256 of a file it had just written and asserted that it
# differed from the digest recorded in COMPLETE. That is true by construction:
# a file whose content you chose will not hash to a digest you did not choose.
# The assertion could not fail. Measured: the control still printed PASS with
# the real gate deleted from the script outright, because the real gate was
# never on the path the control exercised.
#
# WHAT REPLACES IT
# ----------------
# One entry point, ntm_validate_set(), used by builder read-back, remote
# selection, the restore drill, scheduled verification, and every negative
# control. There is exactly one implementation of "is this set trustworthy",
# so the consumers cannot drift apart, and a control that does not call it is
# visibly not testing anything.
#
# Every rejection carries a STRUCTURED FAILURE CLASS in NTM_ERR and the STAGE
# it was rejected at in NTM_STAGE, so a control can assert WHY a set was
# refused rather than merely that it was. "Any non-zero status" is the C-2/C-3
# defect and is not accepted anywhere here.
#
# NTM_STAGES_RUN records the stages that actually executed, in order. That is
# what lets a control assert that later stages did NOT run — i.e. that the
# rejection happened where it was supposed to, and that nothing downstream got
# a chance to look at attacker-chosen bytes.
#
# ----------------------------------------------------------------------------
# THE TRUST ROOT — AND WHAT IT DOES NOT DO
# ----------------------------------------------------------------------------
# Trust must NOT reduce to a plain hash the attacker can recompute. The legacy
# chain was:
#
#     MANIFEST.json  --sha256-->  COMPLETE:manifest_sha256
#
# Anyone who can write MANIFEST.json can also write COMPLETE, and sha256 is a
# public function, so that chain proves only that two files the attacker wrote
# agree with each other. It is a checksum against bit-rot, not a control
# against tampering, and it was being used as the latter.
#
# What is used instead: a KEYED AUTHENTICATED ENVELOPE. The COMMIT marker
# carries a canonical JSON "binding block" over every field that makes this set
# this set —
#
#     set id, component inventory (name+size+ciphertext digest), component
#     count, source snapshot digest, source-time commitment digest, catalogue
#     digest, producer identity, schema version, remote identity, readiness
#     state, and the whole-manifest digest
#
# — together with HMAC-SHA256 over those exact bytes under a key derived from a
# COMPONENT KEY FILE that is never published and never leaves the host:
#
#     K_set = HMAC-SHA256( SHA256(keyfile bytes), "ntm/v1/envelope-key|" || set_id )
#     mac   = HMAC-SHA256( K_set,                 "ntm/v1/envelope|"     || binding )
#
# Each component additionally carries a per-set tag:
#
#     tag   = HMAC-SHA256( K_set, "ntm/v1/component|" || set_id || "|" || name
#                                 || "|" || size || "|" || ciphertext_sha256 )
#
# Deriving K_set per set id is what makes cross-set replay PROVABLE rather than
# merely suspicious: a component or marker lifted from another genuine set
# still verifies — under that other set's key — so the validator can say
# "this is authentic, and it belongs to set X, not set Y" instead of the
# useless "something does not match".
#
# HONEST STATEMENT OF WHAT THIS DEFENDS AGAINST.
#
#   It DOES defend against an attacker who has write access to the object
#   store (the R2 bucket, either remote, or both) but does NOT have the
#   component key file. Such an attacker can delete objects and can substitute
#   bytes, but cannot produce a binding block that verifies. Every substitution
#   — rewritten manifest with a recomputed sha256 included — lands on
#   envelope_mac_mismatch. That is the property the legacy chain did not have.
#
#   It is TAMPER-EVIDENT, NOT A SIGNATURE. This is a symmetric MAC. The
#   verifier holds the same key as the producer, so anyone who can verify can
#   also forge. It carries no non-repudiation and identifies no author. Do not
#   describe it as "signed" anywhere.
#
#   It does NOT defend against: a compromised builder host; a compromised
#   verifier host; anyone who obtains the key file (root on the homelab, a
#   backup of /srv/homelab/secrets, an insider); or key reuse across a
#   producer you did not intend to trust.
#
#   It does NOT provide freshness or anti-rollback on its own. A genuine,
#   correctly authenticated OLD set stays valid forever. Deleting the newest
#   set and leaving an older one is invisible to any per-set check. Freshness
#   must come from outside: the caller states which set id it expects, and
#   NTM_MIN_COMMIT_UTC can floor the commit time. Both are inputs, not
#   discoveries.
#
#   It does NOT prove the set restores. That is the restore drill's job. This
#   validates the manifest and marker surface only.
#
# PARSE-BEFORE-AUTHENTICATE, DELIBERATELY.
# The binding block must be located and canonicalised before its MAC can be
# checked, so a small amount of parsing necessarily precedes authentication.
# That parser is therefore kept total and non-executing: pure json.loads with
# an object_pairs_hook, no eval, no shell interpolation of any manifest value,
# and every string constrained by a closed schema before it is ever emitted
# toward the shell. Nothing from the manifest is executed, globbed or split.
#
# STAGE ORDER, AND WHY THE WHOLE-MANIFEST DIGEST IS CHECKED LAST.
# Stages run in the order given by NTM_STAGE_ORDER. The envelope is
# authenticated early, at stage `envelope`, so every later comparison is made
# against an AUTHENTICATED reference rather than against attacker-chosen bytes.
# Within the final `bindings` stage the individual binding fields are compared
# BEFORE the whole-manifest digest, on purpose: any manifest tamper changes the
# whole-manifest digest too, so checking that first would collapse every
# diagnosis into one class. Field-first means the validator can name the field
# — wrong catalogue digest, wrong source snapshot, wrong source-time
# commitment — and the whole-manifest digest remains as the catch-all for
# everything the field list does not cover. No trust decision is returned early
# either way: ntm_validate_set returns 0 only when every stage passed.
#
# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
# Consumers (validate):
#   ntm_validate_set <set_dir> <expected_set_id> <remote_identity> <keyfile> <depth>
#       depth=full     validate everything, including component bytes on disk
#       depth=markers  manifest + markers + envelope + bindings only; the
#                      component inventory and the component bytes are NOT
#                      examined. For remote selection, where downloading every
#                      component of every candidate set is the thing you are
#                      trying to avoid. There is no default: a caller must say
#                      which it wants, so the cheap answer cannot be obtained
#                      by omission. On success NTM_LEVEL records which was run;
#                      a caller that needs the full answer must check it.
#
# Producers (builder side — the same code paths the validator re-derives from):
#   ntm_canonicalize_json <in> <out>
#   ntm_seal_manifest     <in> <out> <keyfile>
#   ntm_write_ready       <set_dir> <set_id> <remote> <components_verified> <ready_utc>
#   ntm_write_commit      <set_dir> <remote> <commit_utc> <keyfile>
#   ntm_component_tag     <keyfile> <set_id> <name> <size> <ciphertext_sha256>
#
# Outputs (always set, on success and on failure):
#   NTM_ERR          '' on success, else the failure class
#   NTM_ERR_DETAIL   human detail, printable ASCII, never secret material
#   NTM_STAGE        stage the run stopped at ('done' on success)
#   NTM_STAGES_RUN   space-separated stages that executed, in order
#   NTM_LEVEL        'full' | 'markers' (on success)
#   NTM_SET_ID NTM_REMOTE NTM_READY_UTC NTM_COMMIT_UTC
#   NTM_COMPONENT_COUNT NTM_MANIFEST_SHA256
#   NTV_NEG_CLASS    mirror of NTM_ERR, so nt-verify.sh's ntv_negative can
#                    assert the class without any adapter at the call site
#
# Optional inputs (env; unset means "not asserted" unless a default is given):
#   NTM_NOW                    epoch seconds for freshness (default: now)
#   NTM_READY_MAX_AGE          seconds, default 172800 (48h)
#   NTM_READY_COMMIT_MAX_GAP   seconds, default 3600 (1h)
#   NTM_MIN_COMMIT_UTC         RFC3339Z floor on commit_utc (anti-rollback)
#   NTM_SCHEMA_VERSIONS        space list, default "3"
#   NTM_TOOL_VERSIONS          space list, default "" = any
#   NTM_EXPECT_PRODUCER        "host|tool|tool_version", default "" = any
#   NTM_TMP                    scratch parent (must be tmpfs in production)
#
# This file defines functions and constants only. It installs no traps, sets no
# shell options, and runs nothing at source time beyond building its helper
# program, so it is safe to source from both the builder and the drill.
# ============================================================================

NTM_SCHEMA_VERSION=3
NTM_ENVELOPE_VERSION=1
NTM_MARKER_MANIFEST=MANIFEST.json
NTM_MARKER_READY=READY
NTM_MARKER_COMMIT=COMMIT

# Canonical stage order. Consumers and tests use this to assert that a
# rejection happened where it should have, and that nothing after it ran.
NTM_STAGE_ORDER=(layout manifest markers envelope identity ready inventory components bindings done)

NTM_ERR=''
NTM_ERR_DETAIL=''
NTM_STAGE=''
NTM_STAGES_RUN=''
NTM_LEVEL=''

_ntm_stage(){ NTM_STAGE="$1"; NTM_STAGES_RUN="${NTM_STAGES_RUN:+$NTM_STAGES_RUN }$1"; }
_ntm_fail(){  NTM_ERR="$1"; NTM_ERR_DETAIL="${2:-}"; return 1; }

# Did a named stage execute in the last validation?
ntm_stage_ran(){ [[ " $NTM_STAGES_RUN " == *" $1 "* ]]; }

# ============================================================================
# Helper program. All JSON handling, all schema enforcement, all key
# derivation and all MAC arithmetic live here.
#
# Key material NEVER appears in argv: the helper is given the PATH of the key
# file and reads it itself. The old vault-commitment code put an HMAC key in
# `docker exec` argv, where every user on the host could read it out of
# /proc/<pid>/cmdline; that mistake is not repeated.
#
# Every subcommand prints "OK" (optionally followed by shell-safe key=value
# lines) or "ERR <class> <detail>", and exits 0 either way, so a caller cannot
# confuse "the helper crashed" with "the set was rejected". A non-zero exit
# from the helper is itself a distinct class: validator_internal_error.
#
# The subcommands are deliberately COARSE. A full validation is five helper
# invocations, not fifteen: on this class of host a python3 start with these
# imports costs ~100ms, and a validator nobody can afford to run in a loop is a
# validator that quietly stops being run in negative controls. Timestamp
# arithmetic is done in the shell with date(1) for the same reason — every
# timestamp has already been pinned to ^\d{4}-..Z by the marker/schema parse
# before date(1) ever sees it.
# ============================================================================
_NTM_PY=$(cat <<'NTM_PY_EOF'
import base64, hashlib, hmac, json, os, re, sys

def emit_err(cls, detail=""):
    detail = re.sub(r"[^ -~]", "?", str(detail))[:300]
    sys.stdout.write("ERR %s %s\n" % (cls, detail))
    sys.exit(0)

def emit_ok(lines=()):
    sys.stdout.write("OK\n")
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.exit(0)

# ── shapes ──────────────────────────────────────────────────────────────────
# Every string in the manifest is constrained. That is not decoration: values
# that pass the schema are emitted toward a shell, so "printable, bounded, no
# whitespace where whitespace would be a field separator" is a safety property,
# not a style preference.
HEX64  = re.compile(r"\A[0-9a-f]{64}\Z")
SETID  = re.compile(r"\Ant-v2-[0-9]{8}T[0-9]{6}Z\Z")
TS     = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
NAME   = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
REMOTE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_./-]{0,95}\Z")
HOST   = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
VER    = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
PGVER  = re.compile(r"\A[0-9][A-Za-z0-9._-]{0,31}\Z")
IMGREF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@-]{0,191}\Z")
IMGID  = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
IMGDIG = re.compile(r"\A(NONE|[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255})\Z")
EXTS   = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._=,+-]{0,2047}\Z")
ABSPTH = re.compile(r"\A/[A-Za-z0-9._/-]{1,255}\Z")
MODE   = re.compile(r"\A[0-7]{3,4}\Z")
OWNER  = re.compile(r"\A[0-9]{1,10}:[0-9]{1,10}\Z")
TEXT   = re.compile(r"\A[!-~][ -~]{0,511}\Z")
CENSUS = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
B64    = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")

# ── canonical JSON ──────────────────────────────────────────────────────────
# One serialisation, byte-exact: sorted keys, no insignificant whitespace,
# ASCII-escaped, one trailing newline. A manifest whose bytes differ from this
# is rejected rather than silently re-serialised, because "the digest is over
# the bytes I would have written" and "the digest is over the bytes that are
# there" must be the same statement.
def canon_bytes(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("utf-8")

class DupKey(Exception):
    pass

def no_dups(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise DupKey(k)
        out[k] = v
    return out

def load_json_strict(path, missing_cls, empty_cls, parse_cls, dup_cls, canon_cls):
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        emit_err(missing_cls, path)
    except IsADirectoryError:
        emit_err(parse_cls, "is a directory")
    except OSError as e:
        emit_err(parse_cls, "unreadable: " + e.__class__.__name__)
    if len(raw) == 0:
        emit_err(empty_cls, path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        emit_err(parse_cls, "not valid utf-8")
    try:
        obj = json.loads(text, object_pairs_hook=no_dups)
    except DupKey as e:
        emit_err(dup_cls, "duplicate key: " + str(e)[:80])
    except ValueError as e:
        emit_err(parse_cls, str(e)[:120])
    if not isinstance(obj, dict):
        emit_err(parse_cls, "top level is not a JSON object")
    if canon_bytes(obj) != raw:
        emit_err(canon_cls, "bytes are not the canonical serialisation")
    return obj, raw

# ── closed-schema enforcement ───────────────────────────────────────────────
# Check order per field is fixed and matters: present -> right type -> not
# empty -> well formed. Collapsing any two of those loses the ability to tell
# an omission from a typo from a truncation.
def is_int(v):  return isinstance(v, int) and not isinstance(v, bool)
def is_bool(v): return isinstance(v, bool)

def check_field(path, v, fs):
    kind = fs[0]
    if kind in ("str", "setid", "pathname"):
        if not isinstance(v, str):
            emit_err("field_type", path + ": expected string")
        if v == "":
            emit_err("field_empty", path)
        if not fs[1].match(v):
            if kind == "setid":
                emit_err("set_id_malformed", path + ": " + v[:64])
            if kind == "pathname":
                emit_err("unsafe_path", path + ": " + v[:64])
            emit_err("field_malformed", path + ": " + v[:64])
    elif kind == "int":
        if not is_int(v):
            emit_err("field_type", path + ": expected integer")
        if v < fs[1] or v > fs[2]:
            emit_err("field_malformed", path + ": out of range")
    elif kind == "bool":
        if not is_bool(v):
            emit_err("field_type", path + ": expected boolean")
    elif kind == "truebool":
        if not is_bool(v):
            emit_err("field_type", path + ": expected boolean")
        if v is not True:
            emit_err("field_malformed", path + ": must be true")
    elif kind == "obj":
        if not isinstance(v, dict):
            emit_err("field_type", path + ": expected object")
        if not v:
            emit_err("field_empty", path)
        check_obj(path, v, fs[1])
    elif kind == "strlist":
        if not isinstance(v, list):
            emit_err("field_type", path + ": expected array")
        if not v:
            emit_err("field_empty", path)
        seen = set()
        for i, e in enumerate(v):
            sub = "%s[%d]" % (path, i)
            check_field(sub, e, ("str", fs[1]))
            if e in seen:
                emit_err("manifest_duplicate_field", sub + ": repeated value")
            seen.add(e)
    elif kind == "map_int":
        if not isinstance(v, dict):
            emit_err("field_type", path + ": expected object")
        if not v:
            emit_err("field_empty", path)
        for k2, v2 in v.items():
            if not CENSUS.match(k2):
                emit_err("field_malformed", path + ": bad key " + str(k2)[:40])
            if not is_int(v2):
                emit_err("field_type", path + "." + k2 + ": expected integer")
            if v2 < 0:
                emit_err("field_malformed", path + "." + k2 + ": negative")
    elif kind == "counts":
        # Deliberately an OPEN map: its keys are production table names and a
        # closed list here would have to be edited for every new table, which
        # is how closed lists rot into being weakened. The VALUES are still
        # typed, and the whole block is covered by the binding block's
        # source_snapshot_sha256, so nothing in here is unauthenticated.
        if not isinstance(v, dict):
            emit_err("field_type", path + ": expected object")
        if not v:
            emit_err("field_empty", path)
        for k2, v2 in v.items():
            if not CENSUS.match(k2):
                emit_err("field_malformed", path + ": bad key " + str(k2)[:40])
            if is_int(v2) or is_bool(v2):
                continue
            if isinstance(v2, dict):
                for k3, v3 in v2.items():
                    if not is_int(v3):
                        emit_err("field_type", path + "." + k2 + "." + str(k3)[:40])
                continue
            emit_err("field_type", path + "." + k2 + ": expected int, bool or object")
    else:
        emit_err("validator_internal_error", "unknown spec kind " + str(kind))

def check_obj(path, obj, spec):
    for k in obj:
        if k not in spec:
            emit_err("field_unknown", path + "." + str(k)[:64])
    for k, fs in spec.items():
        if k not in obj:
            emit_err("field_missing", path + "." + k)
        check_field(path + "." + k, obj[k], fs)

COMPONENT_SPEC = {
    "name":              ("pathname", NAME),
    "plaintext_sha256":  ("str", HEX64),
    "ciphertext_sha256": ("str", HEX64),
    "ciphertext_size":   ("int", 1, 1 << 62),
    "key":               ("str", NAME),
    "bound_set_id":      ("setid", SETID),
    "set_binding_hmac":  ("str", HEX64),
}

MANIFEST_SPEC = {
    "set_id":                  ("setid", SETID),
    # The accepted-version LIST is enforced at the identity stage, not here.
    # Here it only has to be a plausible version number, so that "this set was
    # built by a newer tool" is reported as schema_version_unsupported rather
    # than as a malformed field.
    "manifest_schema_version": ("int", 1, 99),
    "recovery_set_version":    ("int", 1, 99),
    "created_utc":             ("str", TS),
    "producer":                ("obj", {
        "host":         ("str", HOST),
        "tool":         ("str", NAME),
        "tool_version": ("str", VER),
    }),
    "remotes":                 ("strlist", REMOTE),
    "source":                  ("obj", {
        "container":         ("str", NAME),
        "database":          ("str", NAME),
        "dump_role":         ("str", NAME),
        "postgres_version":  ("str", PGVER),
        "image_ref":         ("str", IMGREF),
        "image_id":          ("str", IMGID),
        "image_repo_digest": ("str", IMGDIG),
        "extensions":        ("str", EXTS),
    }),
    "fingerprints":            ("obj", {
        "roles_sha256":     ("str", HEX64),
        "catalogue_sha256": ("str", HEX64),
        "catalogue_census": ("map_int",),
    }),
    "vault_commitment":        ("obj", {
        "alg":     ("str", NAME),
        "digest":  ("str", HEX64),
        "key_hex": ("str", HEX64),
        "note":    ("str", TEXT),
    }),
    "archive":                 ("obj", {
        "toc_entries":                   ("int", 1, 1 << 31),
        "sha256":                        ("str", HEX64),
        "size":                          ("int", 1, 1 << 62),
        "toc_declares_vault_table_data": ("truebool",),
        "fully_consumed":                ("truebool",),
        "globals_create_role_count":     ("int", 1, 1 << 20),
    }),
    "snapshot_bound_counts":   ("counts",),
    "root_key":                ("obj", {
        "path":   ("str", ABSPTH),
        "mode":   ("str", MODE),
        "owner":  ("str", OWNER),
        "size":   ("int", 1, 1 << 30),
        "sha256": ("str", HEX64),
    }),
    "encryption":              ("obj", {
        "format":           ("str", TEXT),
        "key_separation":   ("truebool",),
        "db_key_file":      ("str", ABSPTH),
        "rootkey_key_file": ("str", ABSPTH),
    }),
    "components":              ("components",),
    "restore_order":           ("strlist", TEXT),
}

def check_manifest(obj):
    for k in obj:
        if k not in MANIFEST_SPEC:
            emit_err("field_unknown", "manifest." + str(k)[:64])
    for k, fs in MANIFEST_SPEC.items():
        if k not in obj:
            emit_err("field_missing", "manifest." + k)
        if fs[0] == "components":
            v = obj[k]
            if not isinstance(v, list):
                emit_err("field_type", "manifest.components: expected array")
            if not v:
                emit_err("field_empty", "manifest.components")
            for i, e in enumerate(v):
                if not isinstance(e, dict):
                    emit_err("field_type", "manifest.components[%d]: expected object" % i)
                check_obj("manifest.components[%d]" % i, e, COMPONENT_SPEC)
            continue
        check_field("manifest." + k, obj[k], fs)

# ── derived digests ─────────────────────────────────────────────────────────
def inventory_sha(components):
    h = hashlib.sha256()
    for c in sorted(components, key=lambda x: x["name"]):
        h.update(("%s\x1f%d\x1f%s\n" % (c["name"], c["ciphertext_size"],
                                        c["ciphertext_sha256"])).encode("utf-8"))
    return h.hexdigest()

def source_snapshot_sha(d):
    block = {
        "archive":               d["archive"],
        "root_key":              d["root_key"],
        "snapshot_bound_counts": d["snapshot_bound_counts"],
        "source":                d["source"],
    }
    return hashlib.sha256(canon_bytes(block)).hexdigest()

def producer_str(d):
    p = d["producer"]
    return "%s|%s|%s" % (p["host"], p["tool"], p["tool_version"])

# ── keys and MACs ───────────────────────────────────────────────────────────
# The key file is read here, by path. Its bytes are never printed, never put
# in argv and never returned to the shell.
def load_key(keyfile, set_id):
    try:
        raw = open(keyfile, "rb").read()
    except OSError:
        emit_err("keyfile_unusable", "cannot read key file")
    if not raw.strip():
        emit_err("keyfile_unusable", "key file is empty")
    if len(raw.strip()) < 16:
        emit_err("keyfile_unusable", "key file has less than 16 bytes of material")
    base = hashlib.sha256(raw).digest()
    return hmac.new(base, b"ntm/v1/envelope-key|" + set_id.encode("utf-8"),
                    hashlib.sha256).digest()

def component_tag(key, set_id, name, size, csha):
    msg = ("ntm/v1/component|%s|%s|%d|%s" % (set_id, name, size, csha)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()

def envelope_mac(key, binding_bytes):
    return hmac.new(key, b"ntm/v1/envelope|" + binding_bytes, hashlib.sha256).hexdigest()

# ── markers ─────────────────────────────────────────────────────────────────
MARKER_SPEC = {
    "READY": {
        "ntm_marker":          re.compile(r"\AREADY\Z"),
        "envelope_version":    re.compile(r"\A[0-9]{1,3}\Z"),
        "set_id":              SETID,
        "remote":              REMOTE,
        "manifest_sha256":     HEX64,
        "components_verified": re.compile(r"\A[0-9]{1,6}\Z"),
        "ready_utc":           TS,
    },
    "COMMIT": {
        "ntm_marker":       re.compile(r"\ACOMMIT\Z"),
        "envelope_version": re.compile(r"\A[0-9]{1,3}\Z"),
        "set_id":           SETID,
        "remote":           REMOTE,
        "ready_utc":        TS,
        "commit_utc":       TS,
        "mac_alg":          re.compile(r"\Ahmac-sha256\Z"),
        "binding_b64":      B64,
        "mac":              HEX64,
    },
}
MARKER_CLS = {
    "READY":  ("ready_missing",  "ready_empty",  "ready_malformed",  "ready_duplicate_field"),
    "COMMIT": ("commit_missing", "commit_empty", "commit_malformed", "commit_duplicate_field"),
}
KV = re.compile(r"\A([a-z][a-z0-9_]{0,31})=(.*)\Z")

def read_marker(path, kind):
    missing, empty, malformed, dup = MARKER_CLS[kind]
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        emit_err(missing, path)
    except OSError:
        emit_err(malformed, "unreadable")
    if not raw.strip():
        emit_err(empty, path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        emit_err(malformed, "not valid utf-8")
    spec = MARKER_SPEC[kind]
    got = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if line == "":
            continue
        m = KV.match(line)
        if not m:
            emit_err(malformed, "line %d is not key=value" % lineno)
        k, v = m.group(1), m.group(2)
        if k not in spec:
            emit_err(malformed, "unknown key %s" % k)
        if k in got:
            emit_err(dup, "repeated key %s" % k)
        if not spec[k].match(v):
            emit_err(malformed, "%s has a malformed value" % k)
        got[k] = v
    for k in spec:
        if k not in got:
            emit_err(malformed, "missing key %s" % k)
    return got

# ── binding block ───────────────────────────────────────────────────────────
BINDING_KEYS = (
    "binding_version", "catalogue_sha256", "component_count", "inventory_sha256",
    "manifest_sha256", "producer", "ready_components_verified", "ready_utc",
    "remote", "schema_version", "set_id", "source_commitment_sha256",
    "source_snapshot_sha256",
)

def build_binding(d, raw, remote, ready_utc, ready_verified):
    return {
        "binding_version":           1,
        "catalogue_sha256":          d["fingerprints"]["catalogue_sha256"],
        "component_count":           len(d["components"]),
        "inventory_sha256":          inventory_sha(d["components"]),
        "manifest_sha256":           hashlib.sha256(raw).hexdigest(),
        "producer":                  producer_str(d),
        "ready_components_verified": ready_verified,
        "ready_utc":                 ready_utc,
        "remote":                    remote,
        "schema_version":            d["manifest_schema_version"],
        "set_id":                    d["set_id"],
        "source_commitment_sha256":  d["vault_commitment"]["digest"],
        "source_snapshot_sha256":    source_snapshot_sha(d),
    }

# Field-first, whole-manifest-digest last. See the header note on stage order:
# this is what makes the diagnosis name a field instead of shrugging.
BIND_CLS = (
    ("binding_version",           "envelope_version_unsupported"),
    ("set_id",                    "set_id_mismatch"),
    ("remote",                    "remote_identity_mismatch"),
    ("producer",                  "producer_identity_mismatch"),
    ("schema_version",            "schema_version_unsupported"),
    ("ready_utc",                 "commit_inconsistent"),
    ("ready_components_verified", "commit_inconsistent"),
    ("component_count",           "binding_inventory_mismatch"),
    ("inventory_sha256",          "binding_inventory_mismatch"),
    ("source_snapshot_sha256",    "binding_source_snapshot_mismatch"),
    ("catalogue_sha256",          "binding_catalogue_digest_mismatch"),
    ("source_commitment_sha256",  "binding_source_commitment_mismatch"),
    ("manifest_sha256",           "binding_manifest_digest_mismatch"),
)

# ── subcommands ─────────────────────────────────────────────────────────────
# One call, everything the manifest can tell us. The identity comparisons that
# used to live in a second invocation are done by the caller against these
# values instead: every one of them has already been matched against a
# whitespace-free pattern, so the shell may split these lines safely.
def sub_schema(argv):
    path = argv[0]
    d, raw = load_json_strict(path, "manifest_missing", "manifest_empty",
                              "manifest_not_json", "manifest_duplicate_field",
                              "manifest_non_canonical")
    check_manifest(d)
    lines = [
        "set_id=" + d["set_id"],
        "schema_version=%d" % d["manifest_schema_version"],
        "created_utc=" + d["created_utc"],
        "manifest_sha256=" + hashlib.sha256(raw).hexdigest(),
        "component_count=%d" % len(d["components"]),
        "catalogue_sha256=" + d["fingerprints"]["catalogue_sha256"],
        "source_commitment_sha256=" + d["vault_commitment"]["digest"],
        "source_snapshot_sha256=" + source_snapshot_sha(d),
        "inventory_sha256=" + inventory_sha(d["components"]),
        "producer=" + producer_str(d),
        "tool_version=" + d["producer"]["tool_version"],
        "remotes=" + " ".join(d["remotes"]),
    ]
    for c in d["components"]:
        lines.append("comp=%s %d %s %s %s" % (c["name"], c["ciphertext_size"],
                                              c["ciphertext_sha256"],
                                              c["bound_set_id"],
                                              c["set_binding_hmac"]))
    emit_ok(lines)

# Both markers in one call, prefixed so the caller cannot mix them up.
def sub_markers(argv):
    r = read_marker(argv[0], "READY")
    c = read_marker(argv[1], "COMMIT")
    lines = ["ready_" + k + "=" + v for k, v in sorted(r.items())]
    lines += ["commit_" + k + "=" + v for k, v in sorted(c.items())]
    emit_ok(lines)

# Decode, canonicalise, shape-check and AUTHENTICATE the committed binding
# block in one step, so no caller can hold a decoded-but-unauthenticated
# binding block and be tempted to read a field out of it.
def sub_envelope(argv):
    b64, expect_mac, keyfile, out = argv[0], argv[1], argv[2], argv[3]
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        emit_err("envelope_binding_unreadable", "binding_b64 is not valid base64")
    try:
        obj = json.loads(blob.decode("utf-8"), object_pairs_hook=no_dups)
    except DupKey:
        emit_err("envelope_binding_unreadable", "binding block has duplicate keys")
    except Exception:
        emit_err("envelope_binding_unreadable", "binding block is not JSON")
    if not isinstance(obj, dict):
        emit_err("envelope_binding_unreadable", "binding block is not an object")
    if canon_bytes(obj) != blob:
        emit_err("envelope_binding_non_canonical", "binding block is not canonical")
    if set(obj.keys()) != set(BINDING_KEYS):
        emit_err("envelope_binding_unreadable", "binding block has the wrong key set")
    if obj["binding_version"] != 1:
        emit_err("envelope_version_unsupported", "binding version %r" % (obj["binding_version"],))
    if not isinstance(obj["set_id"], str) or not SETID.match(obj["set_id"]):
        emit_err("envelope_binding_unreadable", "binding block has a malformed set id")
    key = load_key(keyfile, obj["set_id"])
    if not hmac.compare_digest(envelope_mac(key, blob), expect_mac):
        emit_err("envelope_mac_mismatch",
                 "the committed binding block does not authenticate under the component key")
    open(out, "wb").write(blob)
    emit_ok(["binding_set_id=" + obj["set_id"]])

# Every component's per-set tag in one call. Records file lines are
# "<bound_set_id> <name> <size> <ciphertext_sha256> <tag>".
def sub_compsverify(argv):
    keyfile, records = argv[0], argv[1]
    keys = {}
    for line in open(records).read().splitlines():
        if not line.strip():
            continue
        bound, name, size, csha, tag = line.split(" ")
        if bound not in keys:
            keys[bound] = load_key(keyfile, bound)
        if not hmac.compare_digest(component_tag(keys[bound], bound, name, int(size), csha), tag):
            emit_err("component_binding_invalid", "%s: per-set tag does not authenticate" % name)
    emit_ok()

def sub_comptag(argv):
    keyfile, set_id, name, size, csha = argv[0], argv[1], argv[2], argv[3], argv[4]
    key = load_key(keyfile, set_id)
    emit_ok(["tag=" + component_tag(key, set_id, name, int(size), csha)])

# Recompute the binding block from the manifest, the READY marker and the
# remote being validated, then compare it with the AUTHENTICATED committed
# block, field by field, in the order that yields the most specific class.
def sub_bindcheck(argv):
    manifest, remote, ready_file, committed_file = argv[0], argv[1], argv[2], argv[3]
    d, raw = load_json_strict(manifest, "manifest_missing", "manifest_empty",
                              "manifest_not_json", "manifest_duplicate_field",
                              "manifest_non_canonical")
    check_manifest(d)
    r = read_marker(ready_file, "READY")
    recomputed = build_binding(d, raw, remote, r["ready_utc"], int(r["components_verified"]))
    committed = json.loads(open(committed_file, "rb").read().decode("utf-8"))
    if set(recomputed.keys()) != set(committed.keys()):
        emit_err("envelope_binding_unreadable", "binding key sets differ")
    for field, cls in BIND_CLS:
        if recomputed[field] != committed[field]:
            emit_err(cls, "%s: the set says %r, the authenticated commitment says %r"
                          % (field, recomputed[field], committed[field]))
    emit_ok()

def sub_canon(argv):
    src = open(argv[0], "rb").read()
    obj = json.loads(src.decode("utf-8"), object_pairs_hook=no_dups)
    open(argv[1], "wb").write(canon_bytes(obj))
    emit_ok()

def sub_seal(argv):
    src, out, keyfile = argv[0], argv[1], argv[2]
    obj = json.loads(open(src, "rb").read().decode("utf-8"), object_pairs_hook=no_dups)
    set_id = obj.get("set_id", "")
    if not isinstance(set_id, str) or not SETID.match(set_id):
        emit_err("set_id_malformed", str(set_id)[:64])
    comps = obj.get("components")
    if not isinstance(comps, list) or not comps:
        emit_err("field_empty", "manifest.components")
    for c in comps:
        if not isinstance(c, dict):
            emit_err("field_type", "manifest.components[]: expected object")
        bound = c.get("bound_set_id", set_id)
        if not isinstance(bound, str) or not SETID.match(bound):
            emit_err("set_id_malformed", "components[].bound_set_id")
        c["bound_set_id"] = bound
        key = load_key(keyfile, bound)
        c["set_binding_hmac"] = component_tag(key, bound, c["name"],
                                              int(c["ciphertext_size"]),
                                              c["ciphertext_sha256"])
    open(out, "wb").write(canon_bytes(obj))
    emit_ok()

def sub_mkcommit(argv):
    manifest, ready_file, remote, commit_utc, keyfile, out = argv[:6]
    d, raw = load_json_strict(manifest, "manifest_missing", "manifest_empty",
                              "manifest_not_json", "manifest_duplicate_field",
                              "manifest_non_canonical")
    check_manifest(d)
    r = read_marker(ready_file, "READY")
    b = build_binding(d, raw, remote, r["ready_utc"], int(r["components_verified"]))
    blob = canon_bytes(b)
    key = load_key(keyfile, d["set_id"])
    mac = envelope_mac(key, blob)
    body = (
        "ntm_marker=COMMIT\n"
        "envelope_version=1\n"
        "set_id=%s\n"
        "remote=%s\n"
        "ready_utc=%s\n"
        "commit_utc=%s\n"
        "mac_alg=hmac-sha256\n"
        "binding_b64=%s\n"
        "mac=%s\n"
    ) % (d["set_id"], remote, r["ready_utc"], commit_utc,
         base64.b64encode(blob).decode("ascii"), mac)
    # Created 0600 at open() time rather than chmod'ed afterwards: the MAC is
    # not a secret, but there is no reason to leave a window in which the
    # marker is world-readable on a shared host.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    emit_ok()

SUBS = {
    "schema": sub_schema, "markers": sub_markers, "envelope": sub_envelope,
    "compsverify": sub_compsverify, "bindcheck": sub_bindcheck,
    "comptag": sub_comptag, "canon": sub_canon, "seal": sub_seal,
    "mkcommit": sub_mkcommit,
}

if len(sys.argv) < 2 or sys.argv[1] not in SUBS:
    sys.stderr.write("nt-manifest helper: unknown subcommand\n")
    sys.exit(64)
SUBS[sys.argv[1]](sys.argv[2:])
NTM_PY_EOF
)

# ── shell side of the helper ────────────────────────────────────────────────
# _ntm_call runs a subcommand, captures its output, and translates it into
# NTM_ERR / NTM_ERR_DETAIL. A helper that crashes is NOT a rejection: it is
# validator_internal_error, a class of its own, so a broken validator can never
# be mistaken for a bad set (or, worse, for a good one).
_ntm_call(){ # _ntm_call <outfile> <subcommand> [args...]
  local out="$1"; shift
  local rc=0 first rest
  python3 -c "$_NTM_PY" "$@" > "$out" 2>"$out.err" || rc=$?
  if [[ $rc -ne 0 ]]; then
    NTM_ERR=validator_internal_error
    NTM_ERR_DETAIL="helper exit $rc: $(tr -d '\n' < "$out.err" | cut -c1-200)"
    return 1
  fi
  first=''
  IFS= read -r first < "$out" || first=''
  if [[ "$first" == OK ]]; then return 0; fi
  if [[ "$first" == "ERR "* ]]; then
    rest="${first#ERR }"
    NTM_ERR="${rest%% *}"
    NTM_ERR_DETAIL="${rest#"$NTM_ERR"}"
    NTM_ERR_DETAIL="${NTM_ERR_DETAIL# }"
    return 1
  fi
  NTM_ERR=validator_internal_error
  NTM_ERR_DETAIL="helper produced unparseable output"
  return 1
}

# Load the helper's shell-safe key=value output. Component records go to an
# array because there are many of them; everything else goes to a map.
declare -A NTM_F
_ntm_absorb(){ # _ntm_absorb <helper_output_file>
  local line k v
  NTM_F=(); NTM_COMPS=()
  while IFS= read -r line; do
    [[ "$line" == OK ]] && continue
    [[ -z "$line" ]] && continue
    k="${line%%=*}"; v="${line#*=}"
    if [[ "$k" == comp ]]; then NTM_COMPS+=("$v"); else NTM_F["$k"]="$v"; fi
  done < "$1"
}

# ── producer helpers (builder side) ─────────────────────────────────────────
ntm_canonicalize_json(){ # <in> <out>
  local s; s="$(mktemp "${NTM_TMP:-${NTV_TMP:-${TMPDIR:-/tmp}}}/ntm-canon.XXXXXX")" || return 1
  _ntm_call "$s" canon "$1" "$2"; local rc=$?
  rm -f "$s" "$s.err"; return $rc
}

ntm_seal_manifest(){ # <in> <out> <keyfile>
  local s; s="$(mktemp "${NTM_TMP:-${NTV_TMP:-${TMPDIR:-/tmp}}}/ntm-seal.XXXXXX")" || return 1
  _ntm_call "$s" seal "$1" "$2" "$3"; local rc=$?
  rm -f "$s" "$s.err"; return $rc
}

ntm_component_tag(){ # <keyfile> <set_id> <name> <size> <ciphertext_sha256> -> hex on stdout
  local s rc=0
  s="$(mktemp "${NTM_TMP:-${NTV_TMP:-${TMPDIR:-/tmp}}}/ntm-tag.XXXXXX")" || return 1
  _ntm_call "$s" comptag "$1" "$2" "$3" "$4" "$5" || rc=$?
  if [[ $rc -eq 0 ]]; then _ntm_absorb "$s"; printf '%s' "${NTM_F[tag]}"; fi
  rm -f "$s" "$s.err"; return $rc
}

# READY attests: every component of this set has been uploaded to THIS remote
# and its remote bytes were verified. It is written before COMMIT and is the
# thing COMMIT commits to. Writing it earlier than that is the "premature
# READY" defect the validator rejects.
# The umask is set inside a subshell, not in the caller's shell: a library
# function that quietly changes the caller's umask for the rest of the run is
# a side effect nobody reads the source to discover.
ntm_write_ready(){ # <set_dir> <set_id> <remote> <components_verified> <ready_utc>
  local f="$1/$NTM_MARKER_READY"
  (
    umask 077
    {
      printf 'ntm_marker=READY\n'
      printf 'envelope_version=%s\n' "$NTM_ENVELOPE_VERSION"
      printf 'set_id=%s\n' "$2"
      printf 'remote=%s\n' "$3"
      printf 'manifest_sha256=%s\n' "$(sha256sum "$1/$NTM_MARKER_MANIFEST" | cut -d' ' -f1)"
      printf 'components_verified=%s\n' "$4"
      printf 'ready_utc=%s\n' "$5"
    } > "$f"
  ) || return 1
  chmod 600 "$f" 2>/dev/null || true
  return 0
}

# COMMIT is the authenticated envelope. It is written LAST, and it is the only
# object in the set whose integrity does not reduce to a public hash.
ntm_write_commit(){ # <set_dir> <remote> <commit_utc> <keyfile>
  local s rc=0
  s="$(mktemp "${NTM_TMP:-${NTV_TMP:-${TMPDIR:-/tmp}}}/ntm-commit.XXXXXX")" || return 1
  _ntm_call "$s" mkcommit "$1/$NTM_MARKER_MANIFEST" "$1/$NTM_MARKER_READY" \
            "$2" "$3" "$4" "$1/$NTM_MARKER_COMMIT" || rc=$?
  rm -f "$s" "$s.err"
  if [[ $rc -eq 0 ]]; then chmod 600 "$1/$NTM_MARKER_COMMIT" 2>/dev/null || true; fi
  return $rc
}

# ============================================================================
# THE validator
# ============================================================================
ntm_validate_set(){ # <set_dir> <expected_set_id> <remote_identity> <keyfile> <depth>
  NTM_ERR=''; NTM_ERR_DETAIL=''; NTM_STAGE=''; NTM_STAGES_RUN=''; NTM_LEVEL=''
  NTM_SET_ID=''; NTM_REMOTE=''; NTM_READY_UTC=''; NTM_COMMIT_UTC=''
  NTM_COMPONENT_COUNT=''; NTM_MANIFEST_SHA256=''
  local scratch rc=0
  scratch="$(mktemp -d "${NTM_TMP:-${NTV_TMP:-${TMPDIR:-/tmp}}}/ntm-v.XXXXXX")" || {
    _ntm_stage layout; NTM_ERR=scratch_unavailable
    NTM_ERR_DETAIL='could not create a private scratch directory'
    NTV_NEG_CLASS="$NTM_ERR"; return 1; }
  chmod 700 "$scratch" 2>/dev/null || true
  _ntm_validate_impl "${1:-}" "${2:-}" "${3:-}" "${4:-}" "${5:-}" "$scratch" || rc=$?
  rm -rf "$scratch"
  if [[ $rc -eq 0 ]]; then
    _ntm_stage done
    NTM_ERR=''; NTM_ERR_DETAIL=''
  fi
  # Mirrored so nt-verify.sh's ntv_negative can assert the class directly.
  NTV_NEG_CLASS="$NTM_ERR"
  return $rc
}

_ntm_validate_impl(){
  local dir="$1" want_set="$2" remote="$3" keyfile="$4" depth="$5" S="$6"
  local o="$S/o"

  # ── layout ────────────────────────────────────────────────────────────────
  _ntm_stage layout
  [[ -n "$dir" && -n "$want_set" && -n "$remote" && -n "$keyfile" ]] \
    || _ntm_fail usage 'ntm_validate_set <set_dir> <set_id> <remote> <keyfile> <depth>' || return 1
  # No default depth. A caller that forgets it gets an error, never the cheap
  # answer: "markers" must be asked for out loud.
  [[ "$depth" == full || "$depth" == markers ]] \
    || _ntm_fail usage_bad_depth "depth must be 'full' or 'markers', got '${depth:-<empty>}'" || return 1
  # The requested set id is concatenated into paths and remote prefixes by
  # every consumer, so it is shape-checked before it is used for anything.
  [[ "$want_set" == */* || "$want_set" == *..* ]] \
    && { _ntm_fail unsafe_path "requested set id contains a path separator or '..': ${want_set:0:64}"; return 1; }
  [[ "$want_set" =~ ^nt-v2-[0-9]{8}T[0-9]{6}Z$ ]] \
    || _ntm_fail set_id_malformed "requested set id: ${want_set:0:64}" || return 1
  [[ "$remote" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_./-]{0,95}$ ]] \
    || _ntm_fail remote_identity_mismatch "malformed remote identity: ${remote:0:64}" || return 1
  [[ -d "$dir" ]] || _ntm_fail set_dir_missing "$dir" || return 1
  [[ -r "$keyfile" && -s "$keyfile" ]] \
    || _ntm_fail keyfile_unusable "component key file is missing, empty or unreadable" || return 1

  # ── manifest: canonical bytes, no duplicate keys, closed schema ───────────
  _ntm_stage manifest
  _ntm_call "$o" schema "$dir/$NTM_MARKER_MANIFEST" || return 1
  _ntm_absorb "$o"
  NTM_SET_ID="${NTM_F[set_id]}"
  NTM_MANIFEST_SHA256="${NTM_F[manifest_sha256]}"
  NTM_COMPONENT_COUNT="${NTM_F[component_count]}"
  local created_utc="${NTM_F[created_utc]}"
  local m_schema_version="${NTM_F[schema_version]}"
  local m_tool_version="${NTM_F[tool_version]}"
  local m_producer="${NTM_F[producer]}"
  local m_remotes="${NTM_F[remotes]}"
  # NTM_COMPS / NTM_F are overwritten by every later _ntm_absorb, so the
  # component records are kept here rather than re-derived from a second
  # parse. Two parses of the same file is two chances to disagree.
  local -a comps=("${NTM_COMPS[@]}")

  # ── markers: present, parseable, closed key set, no duplicates ────────────
  _ntm_stage markers
  _ntm_call "$o" markers "$dir/$NTM_MARKER_READY" "$dir/$NTM_MARKER_COMMIT" || return 1
  _ntm_absorb "$o"
  local ready_set="${NTM_F[ready_set_id]}"       ready_remote="${NTM_F[ready_remote]}"
  local ready_utc="${NTM_F[ready_ready_utc]}"    ready_msha="${NTM_F[ready_manifest_sha256]}"
  local ready_verified="${NTM_F[ready_components_verified]}"
  local commit_set="${NTM_F[commit_set_id]}"     commit_remote="${NTM_F[commit_remote]}"
  local commit_ready_utc="${NTM_F[commit_ready_utc]}" commit_utc="${NTM_F[commit_commit_utc]}"
  local commit_mac="${NTM_F[commit_mac]}"        commit_b64="${NTM_F[commit_binding_b64]}"
  NTM_READY_UTC="$ready_utc"; NTM_COMMIT_UTC="$commit_utc"

  # ── envelope: the keyed authenticated binding block ───────────────────────
  # This is the trust root. Everything after it compares observed state against
  # an AUTHENTICATED reference rather than against bytes an attacker chose.
  _ntm_stage envelope
  _ntm_call "$o" envelope "$commit_b64" "$commit_mac" "$keyfile" "$S/committed.json" || return 1
  local binding_set
  _ntm_absorb "$o"
  binding_set="${NTM_F[binding_set_id]}"
  # The MAC verified, so this block is genuinely ours — but it may be genuinely
  # ours FOR ANOTHER SET. That is the cross-set replay case, and because the
  # key is derived per set id we can state it as a fact rather than a guess.
  [[ "$binding_set" == "$want_set" ]] || {
    _ntm_fail replay_marker_foreign_set \
      "the COMMIT marker authenticates, but it commits set $binding_set, not $want_set"
    return 1; }

  # ── identity ──────────────────────────────────────────────────────────────
  _ntm_stage identity
  [[ "$NTM_SET_ID" == "$want_set" ]] || {
    _ntm_fail set_id_mismatch \
      "manifest says $NTM_SET_ID, caller asked for $want_set"; return 1; }
  local accepted="${NTM_SCHEMA_VERSIONS:-3}"
  [[ " $accepted " == *" $m_schema_version "* ]] || {
    _ntm_fail schema_version_unsupported \
      "manifest schema $m_schema_version is not in {$accepted}"; return 1; }
  if [[ -n "${NTM_TOOL_VERSIONS:-}" ]]; then
    [[ " $NTM_TOOL_VERSIONS " == *" $m_tool_version "* ]] || {
      _ntm_fail tool_version_unsupported \
        "producer tool version $m_tool_version is not in {$NTM_TOOL_VERSIONS}"; return 1; }
  fi
  if [[ -n "${NTM_EXPECT_PRODUCER:-}" ]]; then
    [[ "$m_producer" == "$NTM_EXPECT_PRODUCER" ]] || {
      _ntm_fail producer_identity_mismatch "manifest says $m_producer"; return 1; }
  fi
  [[ " $m_remotes " == *" $remote "* ]] || {
    _ntm_fail remote_identity_mismatch \
      "$remote is not among the manifest's remotes ($m_remotes)"; return 1; }
  [[ "$commit_set" == "$binding_set" ]] || {
    _ntm_fail commit_inconsistent \
      "COMMIT header names $commit_set but its authenticated binding names $binding_set"
    return 1; }
  [[ "$ready_set" == "$want_set" ]] || {
    _ntm_fail set_id_mismatch "READY names set $ready_set, caller asked for $want_set"; return 1; }
  [[ "$ready_remote" == "$remote" ]] || {
    _ntm_fail remote_identity_mismatch \
      "READY attests remote $ready_remote, this validation is against $remote"; return 1; }
  [[ "$commit_remote" == "$remote" ]] || {
    _ntm_fail remote_identity_mismatch \
      "COMMIT names remote $commit_remote, this validation is against $remote"; return 1; }
  [[ "$ready_msha" == "$NTM_MANIFEST_SHA256" ]] || {
    _ntm_fail commit_inconsistent \
      "READY attests a different manifest than the one present"; return 1; }
  NTM_REMOTE="$remote"

  # ── readiness: ordering and freshness ─────────────────────────────────────
  # Every timestamp reaching date(1) here has already been pinned to
  # ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$ by the marker/schema parse, so this
  # is arithmetic on validated input rather than a second, looser parser.
  _ntm_stage ready
  local created_epoch ready_epoch commit_epoch now
  ready_epoch="$(date -u -d "$ready_utc" +%s)" \
    || { _ntm_fail field_malformed "unparseable ready_utc"; return 1; }
  commit_epoch="$(date -u -d "$commit_utc" +%s)" \
    || { _ntm_fail field_malformed "unparseable commit_utc"; return 1; }
  created_epoch="$(date -u -d "$created_utc" +%s)" \
    || { _ntm_fail field_malformed "unparseable created_utc"; return 1; }

  now="${NTM_NOW:-$(date -u +%s)}"
  [[ "$now" =~ ^[0-9]+$ ]] || _ntm_fail field_malformed "NTM_NOW is not an epoch" || return 1

  # Premature: READY attests a state that did not yet exist when it was
  # written. Either it predates the manifest it claims to have verified, or it
  # claims fewer verified components than the set actually has.
  [[ "$ready_epoch" -ge "$created_epoch" ]] || {
    _ntm_fail ready_premature \
      "READY ($ready_utc) predates the manifest it attests ($created_utc)"; return 1; }
  [[ "$ready_verified" -eq "$NTM_COMPONENT_COUNT" ]] || {
    _ntm_fail ready_premature \
      "READY attests $ready_verified verified components, the manifest lists $NTM_COMPONENT_COUNT"
    return 1; }

  local max_age="${NTM_READY_MAX_AGE:-172800}" max_gap="${NTM_READY_COMMIT_MAX_GAP:-3600}"
  [[ $(( now - ready_epoch )) -le "$max_age" ]] || {
    _ntm_fail ready_stale \
      "READY is $(( now - ready_epoch ))s old, the ceiling is ${max_age}s"; return 1; }
  [[ $(( commit_epoch - ready_epoch )) -le "$max_gap" ]] || {
    _ntm_fail ready_stale \
      "COMMIT trails READY by $(( commit_epoch - ready_epoch ))s, the ceiling is ${max_gap}s"
    return 1; }
  [[ "$commit_ready_utc" == "$ready_utc" ]] || {
    _ntm_fail commit_inconsistent \
      "COMMIT references READY at $commit_ready_utc, the READY present says $ready_utc"; return 1; }
  [[ "$commit_epoch" -ge "$ready_epoch" ]] || {
    _ntm_fail commit_inconsistent "COMMIT predates the READY it references"; return 1; }
  if [[ -n "${NTM_MIN_COMMIT_UTC:-}" ]]; then
    local floor_epoch
    # Caller-supplied, so it is shape-checked here rather than trusted.
    [[ "$NTM_MIN_COMMIT_UTC" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
      || { _ntm_fail field_malformed "NTM_MIN_COMMIT_UTC is not an RFC3339 UTC timestamp"; return 1; }
    floor_epoch="$(date -u -d "$NTM_MIN_COMMIT_UTC" +%s)" \
      || { _ntm_fail field_malformed "unparseable NTM_MIN_COMMIT_UTC"; return 1; }
    [[ "$commit_epoch" -ge "$floor_epoch" ]] || {
      _ntm_fail commit_stale \
        "COMMIT at $commit_utc is older than the caller's floor $NTM_MIN_COMMIT_UTC"; return 1; }
  fi

  # markers depth stops before anything that needs the component bytes. The
  # binding comparison below does not need them, so it still runs.
  if [[ "$depth" == markers ]]; then
    NTM_LEVEL=markers
    _ntm_bindings_stage "$dir" "$remote" "$S" || return 1
    return 0
  fi
  NTM_LEVEL=full

  # ── inventory: the surface must hold exactly what the manifest lists ──────
  # Missing entries are the components stage's business (component_missing) so
  # that "the object was deleted" and "an extra object appeared" stay distinct.
  _ntm_stage inventory
  local -A listed=()
  local rec cname csize csha cbound ctag
  for rec in "${comps[@]}"; do
    read -r cname csize csha cbound ctag <<<"$rec"
    [[ -z "${listed[$cname]:-}" ]] || {
      _ntm_fail inventory_duplicate "component $cname is listed more than once"; return 1; }
    listed["$cname"]=1
  done
  local f base
  while IFS= read -r f; do
    base="$(basename "$f")"
    case "$base" in
      "$NTM_MARKER_MANIFEST"|"$NTM_MARKER_READY"|"$NTM_MARKER_COMMIT") continue ;;
    esac
    [[ -n "${listed[$base]:-}" ]] || {
      _ntm_fail inventory_mismatch "the set surface carries $base, which the manifest does not list"
      return 1; }
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -type f -print | sort)

  # ── components: bytes on the surface vs the manifest record ───────────────
  _ntm_stage components
  local actual_size actual_sha
  : > "$S/records"
  for rec in "${comps[@]}"; do
    read -r cname csize csha cbound ctag <<<"$rec"
    [[ -e "$dir/$cname" ]] || { _ntm_fail component_missing "$cname"; return 1; }
    [[ -f "$dir/$cname" ]] || { _ntm_fail component_missing "$cname is not a regular file"; return 1; }
    actual_size="$(stat -c %s "$dir/$cname")" \
      || { _ntm_fail component_missing "$cname: cannot stat"; return 1; }
    [[ "$actual_size" -gt 0 ]] || { _ntm_fail component_empty "$cname is zero length"; return 1; }
    # Short is TRUNCATED and long is a SIZE MISMATCH. They are different
    # accidents — an interrupted upload versus a substituted object — and
    # folding them together throws away the only cheap clue about which.
    if [[ "$actual_size" -lt "$csize" ]]; then
      _ntm_fail component_truncated "$cname: $actual_size of $csize bytes present"; return 1
    fi
    if [[ "$actual_size" -ne "$csize" ]]; then
      _ntm_fail component_size_mismatch "$cname: $actual_size bytes, manifest says $csize"; return 1
    fi
    actual_sha="$(sha256sum "$dir/$cname" | cut -d' ' -f1)" \
      || { _ntm_fail component_missing "$cname: cannot digest"; return 1; }
    [[ "$actual_sha" == "$csha" ]] || {
      _ntm_fail component_digest_mismatch "$cname: ciphertext digest differs from the manifest"
      return 1; }
    printf '%s %s %s %s %s\n' "$cbound" "$cname" "$csize" "$csha" "$ctag" >> "$S/records"
  done
  # Per-set tags, all of them, in one call. Checked BEFORE the bound-set-id
  # comparison on purpose: a tag that does not authenticate at all is a
  # forgery, while a tag that authenticates under a DIFFERENT set id is a
  # genuine component of that other set. Only the second one is replay, and
  # only this ordering lets the validator tell them apart.
  _ntm_call "$o" compsverify "$keyfile" "$S/records" || return 1
  for rec in "${comps[@]}"; do
    read -r cname csize csha cbound ctag <<<"$rec"
    [[ "$cbound" == "$want_set" ]] || {
      _ntm_fail replay_component_foreign_set \
        "$cname authenticates, but as a component of $cbound, not $want_set"; return 1; }
  done

  # ── bindings: the manifest vs the authenticated commitment ────────────────
  _ntm_bindings_stage "$dir" "$remote" "$S" || return 1
  return 0
}

_ntm_bindings_stage(){ # <dir> <remote> <scratch>
  local dir="$1" remote="$2" S="$3" o="$3/o"
  _ntm_stage bindings
  _ntm_call "$o" bindcheck "$dir/$NTM_MARKER_MANIFEST" "$remote" \
            "$dir/$NTM_MARKER_READY" "$S/committed.json" || return 1
  return 0
}
