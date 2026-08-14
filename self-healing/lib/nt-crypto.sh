# shellcheck shell=bash
# ============================================================================
# nt-crypto.sh — authenticated decryption and archive provenance primitives.
#
# WHY THIS EXISTS (audit findings C-1 and C-4)
# --------------------------------------------
# C-1. The builder proved a component "decrypts" like this:
#
#     verify_decrypts(){
#       got="$(gpg … -d "$1" 2>/dev/null | sha256sum | cut -d' ' -f1)"
#       [[ "$got" == "$3" ]]
#     }
#
#   Three independent defects, all measured, none theoretical:
#
#     (a) gpg's exit status is discarded. In a pipeline only the LAST command's
#         status survives, and `set -e` does not see it. gpg exiting 2 with
#         "encrypted message has been manipulated!" is indistinguishable from
#         gpg exiting 0.
#     (b) stderr is discarded, so the reason is unrecoverable afterwards.
#     (c) the plaintext is handed to a consumer (sha256sum) WHILE gpg is still
#         streaming — that is, before any authentication verdict exists.
#
#   Measured on this project's own material:
#
#     gpg 2.4.4, AEAD/OCB (the format the builder actually produces):
#       5 000 000-byte payload, one flipped byte in the tag region
#       -> gpg exit 2, and 5 000 000 bytes of plaintext already written to
#          stdout before that status was available to anyone.
#
#     gpg 2.2.27, CFB+MDC (the format a host without OCB produces):
#       100 000-byte payload, tag-region flip AND appended-bytes
#       -> gpg exit 2, full plaintext emitted, and the digest still MATCHED,
#          so the legacy gate returned TRUE and accepted the corruption.
#
#   Note carefully what is and is not claimed. Under OCB the digest comparison
#   happens to reject all five corruption classes we tested, because OCB
#   changes the output. That backstop is INCIDENTAL, not designed, and it does
#   not exist under MDC. We therefore do not claim "GnuPG emits zero bytes"
#   and we do not rely on a digest to imply integrity. The property we enforce
#   is narrower and checkable:
#
#       UNAUTHENTICATED PLAINTEXT IS NEVER RELEASED TO A CONSUMER.
#
#   The mechanism is a quarantine: gpg writes to a private mode-0600 file whose
#   path no consumer can see, the exit status is captured unmodified, and the
#   file is destroyed unless every binding check passes. Only after that is it
#   atomically renamed to a validated path, and only that path is returned.
#
# C-4. The builder shipped an archive it had never read to the end:
#       - `docker cp` moved bytes container->host with no digest round-trip,
#         while the root key DID get exactly that treatment;
#       - container_cleanup deleted the in-container original immediately,
#         so no comparison was possible afterwards;
#       - `[[ -s ]]` accepts one byte;
#       - `pg_restore -l` reads only the header and TOC at the FRONT of a -Fc
#         archive, so a truncated archive lists a complete TOC, satisfies the
#         `TABLE DATA vault secrets` grep, and publishes.
#     A TOC entry proves the archive DESCRIBES an item. It proves nothing about
#     whether that item's data bytes are present. The only honest test is full
#     consumption, and the independent oracle is a strict restore.
# ============================================================================

# ── failure classification ──────────────────────────────────────────────────
# Every primitive sets NTC_ERR to a stable machine-readable class so a negative
# control can assert WHY it failed, not merely that it did. `set -e`-style
# "anything non-zero passes" is exactly the defect this replaces.
NTC_ERR=''
NTC_ERR_DETAIL=''
NTC_VALIDATED_PATH=''

ntc_fail(){ NTC_ERR="$1"; NTC_ERR_DETAIL="${2:-}"; return 1; }

# Private scratch. Must be tmpfs in production; the caller sets it.
: "${NTC_QUARANTINE_DIR:=${NTV_TMP:-${TMPDIR:-/tmp}}}"

# ── packet-format binding ───────────────────────────────────────────────────
# Parsed from the header bytes directly rather than by shelling out to
# `gpg --list-packets`, which (a) is another place to lose an exit status and
# (b) hangs on GnuPG 2.2.27 when handed an AEAD packet. RFC 4880 / rfc4880bis:
#   ctb 0x8c -> tag  3, symmetric-key encrypted session key
#   ctb 0xd2 -> tag 18, symmetrically encrypted integrity protected (CFB+MDC)
#   ctb 0xd4 -> tag 20, AEAD encrypted data (OCB/EAX/GCM)
ntc_packet_format(){ # ntc_packet_format <file> -> echoes 'ocb' | 'mdc' | 'unknown'
  local f="$1" hdr
  hdr="$(head -c 2 "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  # A file shorter than the two header bytes cannot be classified. Without this
  # guard the length arithmetic below evaluates `$(( 0x ))` on the empty string,
  # which is a bash arithmetic syntax error rather than a clean verdict.
  [[ ${#hdr} -eq 4 ]]   || { echo unknown; return 0; }
  [[ "$hdr" == 8c* ]]   || { echo unknown; return 0; }
  # skip the tag-3 packet: byte 1 is the one-octet length of its body
  local plen off b
  plen=$(( 0x${hdr:2:2} ))
  off=$(( 2 + plen ))
  b="$(dd if="$f" bs=1 skip=$off count=1 status=none 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  case "$b" in
    d4) echo ocb ;;
    d2) echo mdc ;;
    *)  echo unknown ;;
  esac
}

# ── THE authenticated decryption primitive ──────────────────────────────────
# ntc_decrypt_validated <ciphertext> <passfile> <expected_plaintext_sha256> \
#                       <expected_plaintext_size|-> <expected_format|any> <set_id_binding|->
#
# On success: returns 0, and NTC_VALIDATED_PATH holds a path the caller may
# consume. On ANY failure: returns non-zero, NTC_ERR names the class, no
# validated path exists, and the quarantine file has been removed.
#
# The caller MUST consume only $NTC_VALIDATED_PATH. The quarantine path is
# never exported, so a consumer cannot reach the bytes while they are still
# unauthenticated even by mistake.
ntc_decrypt_validated(){
  local ct="$1" pass="$2" want_sha="$3" want_size="${4:--}" want_fmt="${5:-any}" bind="${6:--}"
  NTC_ERR=''; NTC_ERR_DETAIL=''; NTC_VALIDATED_PATH=''

  [[ -f "$ct" ]]   || { ntc_fail no_ciphertext "$ct"; return 1; }
  [[ -s "$ct" ]]   || { ntc_fail empty_ciphertext "$ct"; return 1; }
  [[ -r "$pass" ]] || { ntc_fail no_passfile "$pass"; return 1; }

  # format binding BEFORE decryption: a downgraded or malformed header is
  # rejected without ever invoking the cipher.
  # Order matters and is asserted by the suite: an UNPARSEABLE header is
  # malformed_header whatever was expected, while a header that parses cleanly
  # as the other supported format is the distinct class format_mismatch (the
  # OCB->MDC downgrade). Collapsing the two would make a corrupt file and a
  # downgraded file indistinguishable to a control.
  local fmt; fmt="$(ntc_packet_format "$ct")"
  if [[ "$fmt" == unknown ]]; then ntc_fail malformed_header ''; return 1; fi
  if [[ "$want_fmt" != any && "$fmt" != "$want_fmt" ]]; then
    ntc_fail format_mismatch "expected $want_fmt, header says $fmt"; return 1
  fi

  local q err rc
  umask 077
  q="$(mktemp "$NTC_QUARANTINE_DIR/ntc-q.XXXXXX")"   || { ntc_fail quarantine_create ''; return 1; }
  err="$(mktemp "$NTC_QUARANTINE_DIR/ntc-e.XXXXXX")" || { rm -f "$q"; ntc_fail quarantine_create ''; return 1; }
  chmod 600 "$q" "$err"

  # -o writes to the quarantine file. Nothing is piped anywhere. The exit
  # status is captured from gpg itself, not from the tail of a pipeline.
  # `|| rc=$?` suspends errexit for the left-hand side only, so the status
  # survives without globally disabling `set -e` for the rest of the function.
  rc=0
  gpg --batch --yes --quiet --no-tty --pinentry-mode loopback \
      --passphrase-file "$pass" -o "$q" -d "$ct" </dev/null 2>"$err" || rc=$?

  NTC_LAST_STDERR="$(head -c 4000 "$err")"
  rm -f "$err"

  # (5) on non-zero status the quarantine is destroyed. This is the single
  # most important line in the file: it is what makes "gpg streamed the whole
  # plaintext" harmless, because the streamed bytes went into a file nobody
  # can name and that file no longer exists.
  if [[ $rc -ne 0 ]]; then
    rm -f "$q"
    ntc_fail gpg_status "exit $rc: $NTC_LAST_STDERR"
    return 1
  fi

  # (6) binding checks, still inside quarantine
  local got_size got_sha
  got_size="$(stat -c %s "$q")"
  if [[ "$want_size" != '-' && "$got_size" != "$want_size" ]]; then
    rm -f "$q"; ntc_fail size_mismatch "expected $want_size got $got_size"; return 1
  fi
  if [[ "$got_size" -eq 0 ]]; then
    rm -f "$q"; ntc_fail empty_plaintext ''; return 1
  fi
  got_sha="$(sha256sum "$q" | cut -d' ' -f1)"
  if [[ "$want_sha" != '-' && "$got_sha" != "$want_sha" ]]; then
    rm -f "$q"; ntc_fail digest_mismatch "expected $want_sha got $got_sha"; return 1
  fi
  if [[ "$bind" != '-' ]]; then
    # cross-set replay guard: the caller passes the set id the component must
    # belong to; the manifest digest it was looked up under already encodes it,
    # so a component lifted from another set fails digest_mismatch above. This
    # records the binding explicitly so the control can assert on it.
    NTC_BOUND_SET="$bind"
  fi

  # (7) atomic promote, same filesystem, only now nameable by a consumer
  local v="${q}.validated"
  mv -f "$q" "$v" || { rm -f "$q"; ntc_fail promote_failed ''; return 1; }
  NTC_VALIDATED_PATH="$v"
  return 0
}

# The caller OWNS the validated file and must release it when done. Making
# this explicit is what lets a control assert that the quarantine directory is
# empty at the end of a run: a leftover validated plaintext is a finding, not
# housekeeping.
ntc_release_validated(){
  [[ -n "${NTC_VALIDATED_PATH:-}" ]] && rm -f "$NTC_VALIDATED_PATH"
  NTC_VALIDATED_PATH=''
  return 0
}

# ── builder-side: encrypt, then prove the ciphertext round-trips ────────────
# Replaces verify_decrypts(). Uses the SAME primitive, so the builder and the
# drill cannot drift apart in what "decrypts correctly" means.
ntc_encrypt_and_verify(){ # <plaintext> <out.gpg> <passfile> <expected_format>
  local pt="$1" out="$2" pass="$3" want_fmt="${4:-ocb}" sha size rc
  NTC_ERR=''; NTC_VALIDATED_PATH=''
  [[ -s "$pt" ]] || { ntc_fail empty_plaintext "$pt"; return 1; }
  sha="$(sha256sum "$pt" | cut -d' ' -f1)"
  size="$(stat -c %s "$pt")"

  rc=0
  gpg --batch --yes --quiet --no-tty --pinentry-mode loopback --passphrase-file "$pass" \
      --symmetric --cipher-algo AES256 --force-ocb --s2k-mode 3 \
      --s2k-digest-algo SHA512 --s2k-count 65011712 -o "$out" "$pt" 2>"$out.err" || rc=$?
  if [[ $rc -ne 0 ]]; then
    NTC_LAST_STDERR="$(head -c 2000 "$out.err")"; rm -f "$out.err" "$out"
    # GnuPG 2.2.x rejects --force-ocb outright ("invalid option"). Failing
    # closed here is correct: a set must never be silently downgraded to MDC,
    # under which the legacy digest backstop demonstrably does not hold.
    ntc_fail encrypt_failed "exit $rc: $NTC_LAST_STDERR"; return 1
  fi
  rm -f "$out.err"

  ntc_decrypt_validated "$out" "$pass" "$sha" "$size" "$want_fmt" '-' || return 1
  # the round-trip proof is complete; the plaintext copy is not needed
  ntc_release_validated
  return 0
}

# ============================================================================
# C-4 — archive provenance and completeness
# ============================================================================

# ntc_archive_roundtrip <container> <in_container_path> <host_path>
# Digest and size are computed INSIDE the producing container, the bytes are
# copied out, and the host copy must match exactly. The in-container original
# is NOT deleted by this function — the caller keeps it until the round trip
# has succeeded, which is precisely what the old container_cleanup made
# impossible.
ntc_archive_roundtrip(){
  local cn="$1" cpath="$2" hpath="$3" csha csize hsha hsize
  NTC_ERR=''
  csha="$(docker exec "$cn" sha256sum "$cpath" 2>/dev/null | cut -d' ' -f1)" \
    || { ntc_fail container_digest_failed ''; return 1; }
  [[ "$csha" =~ ^[0-9a-f]{64}$ ]] || { ntc_fail container_digest_shape "$csha"; return 1; }
  csize="$(docker exec "$cn" stat -c %s "$cpath" 2>/dev/null)" \
    || { ntc_fail container_size_failed ''; return 1; }
  [[ "$csize" =~ ^[0-9]+$ ]] || { ntc_fail container_size_shape "$csize"; return 1; }
  [[ "$csize" -gt 1024 ]] || { ntc_fail archive_implausibly_small "$csize bytes"; return 1; }

  docker cp "$cn:$cpath" "$hpath" >/dev/null 2>&1 || { ntc_fail docker_cp_failed ''; return 1; }

  hsha="$(sha256sum "$hpath" | cut -d' ' -f1)"
  hsize="$(stat -c %s "$hpath")"
  [[ "$hsize" == "$csize" ]] || { ntc_fail roundtrip_size_mismatch "container=$csize host=$hsize"; return 1; }
  [[ "$hsha" == "$csha" ]]   || { ntc_fail roundtrip_digest_mismatch "container=$csha host=$hsha"; return 1; }
  NTC_ARCHIVE_SHA="$csha"; NTC_ARCHIVE_SIZE="$csize"
  return 0
}

# ntc_archive_fully_consumable <image> <host_archive_path>
# The independent oracle for "the archive's DATA bytes are all present".
#
# `pg_restore -l` reads only the header and TOC, which live at the FRONT of a
# custom-format archive — a file truncated anywhere after the TOC still lists
# a complete inventory. `pg_restore -f /dev/null` walks every data block to the
# end, so truncation, a short write or a corrupt block is fatal. Output goes to
# /dev/null rather than a database precisely so this stays a pure read test.
ntc_archive_fully_consumable(){
  local image="$1" arch="$2" d rc
  NTC_ERR=''
  d="$(dirname "$arch")"
  rc=0
  docker run --rm --network none -v "$d:/w:ro" "$image" \
    pg_restore -f /dev/null "/w/$(basename "$arch")" >/dev/null 2>"$arch.consume.err" || rc=$?
  if [[ $rc -ne 0 ]]; then
    NTC_LAST_STDERR="$(head -c 4000 "$arch.consume.err")"; rm -f "$arch.consume.err"
    ntc_fail archive_not_consumable "exit $rc: $NTC_LAST_STDERR"; return 1
  fi
  # a clean exit with diagnostics on stderr is still a classified outcome, not
  # a discarded one
  NTC_CONSUME_STDERR="$(head -c 4000 "$arch.consume.err")"
  rm -f "$arch.consume.err"
  return 0
}

# ntc_archive_declares <image> <host_archive_path> <toc_pattern>
# Deliberately named "declares", not "contains". A TOC entry is a DECLARATION.
# Callers must pair it with ntc_archive_fully_consumable to learn anything
# about the bytes.
ntc_archive_declares(){
  local image="$1" arch="$2" pat="$3" d rc out
  NTC_ERR=''
  d="$(dirname "$arch")"
  rc=0
  out="$(docker run --rm --network none -v "$d:/w:ro" "$image" \
         pg_restore -l "/w/$(basename "$arch")" 2>"$arch.toc.err")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    NTC_LAST_STDERR="$(head -c 2000 "$arch.toc.err")"; rm -f "$arch.toc.err"
    ntc_fail toc_unreadable "exit $rc"; return 1
  fi
  rm -f "$arch.toc.err"
  NTC_TOC_ENTRIES="$(grep -c '^[0-9]' <<<"$out" || true)"
  grep -q "$pat" <<<"$out" || { ntc_fail toc_missing_entry "$pat"; return 1; }
  return 0
}
