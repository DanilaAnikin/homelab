#!/usr/bin/env bash
# ============================================================================
# C-1 — authenticated decryption, red-before-fix.
#
#   NT_IMPL=legacy  reproduces nt-recovery-set.sh:105-110 verbatim and MUST be
#                   red: it releases unauthenticated plaintext to a consumer on
#                   every corruption class, and under CFB+MDC it also ACCEPTS
#                   two of them outright.
#   NT_IMPL=strict  runs ntc_decrypt_validated and MUST be green.
#
# THE PROPERTY UNDER TEST is not "gpg emits zero bytes" — it demonstrably does
# not. It is:
#
#     no consumer ever receives plaintext that has not been authenticated.
#
# So every rejection is asserted against SIX observable downstream effects,
# each of which the real pipeline performs and each of which is represented
# here by a file the consumer would have created:
#
#     validated-path / extraction / restore / upload / COMMIT / heartbeat
#
# A control passes only when the decrypt is rejected with the EXPECTED failure
# class AND all six remain absent. "It returned non-zero" is not sufficient —
# that is the C-2/C-3 defect, and it is not repeated here.
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/nt-crypto.sh
. "$HERE/../lib/nt-crypto.sh"

IMPL="${NT_IMPL:-strict}"
W="$(mktemp -d "${TMPDIR:-/tmp}/c1test.XXXXXX")"
trap 'rm -rf "$W"' EXIT
export GNUPGHOME="$W/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
export NTC_QUARANTINE_DIR="$W/q"; mkdir -p "$NTC_QUARANTINE_DIR"; chmod 700 "$NTC_QUARANTINE_DIR"

KEY="$W/key.txt";   head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$KEY";   chmod 600 "$KEY"
KEY2="$W/key2.txt"; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$KEY2"; chmod 600 "$KEY2"

OK=0; BAD=0
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }

# ── the six downstream effects a consumer would produce ─────────────────────
SIDE=("$W/fx.extracted" "$W/fx.restored" "$W/fx.uploaded" "$W/fx.commit" "$W/fx.heartbeat")
reset_effects(){ rm -f "${SIDE[@]}" "$W/fx.validated" "$W/fx.released"; }
effects_seen(){ local s; for s in "${SIDE[@]}" "$W/fx.validated"; do [[ -e "$s" ]] && { echo "${s##*/}"; }; done; }

# ── the measurement that matters on BOTH formats ────────────────────────────
# Under CFB+MDC the legacy gate ACCEPTS a tag-region flip, so the corruption
# reaches the six stages above and the leak is obvious. Under AEAD/OCB the
# legacy gate happens to REJECT every corruption we can construct, because OCB
# changes the plaintext and the digest therefore differs. That incidental
# backstop makes the six-effect check silent on OCB — and it would be easy, and
# wrong, to read that silence as "the production path is fine".
#
# It is not fine. The defect on OCB is that gpg's output is PIPED to sha256sum,
# a live consumer process, and gpg streams the entire decrypted payload into it
# before the authentication tag is ever checked. So we instrument the consumer
# itself and count the bytes it was handed. For a corrupt component the correct
# answer is zero, on every format.
released_bytes(){ [[ -f "$W/fx.released" ]] && cat "$W/fx.released" || echo 0; }

# A consumer that behaves exactly as the real pipeline does: given a plaintext
# path it extracts, restores, uploads, commits and beats. It cannot tell
# whether the bytes were authenticated — that is the caller's job, which is the
# entire point.
consume(){ # consume <plaintext_path>
  [[ -s "$1" ]] || return 0
  : > "$W/fx.validated"
  tar -tzf "$1" >/dev/null 2>&1 || true;  : > "$W/fx.extracted"
  : > "$W/fx.restored"; : > "$W/fx.uploaded"; : > "$W/fx.commit"; : > "$W/fx.heartbeat"
}

# ── legacy implementation, byte-for-byte from nt-recovery-set.sh:105-110 ────
legacy_gate(){ # <ct> <passfile> <expected_sha> ; ignores size/format/binding
  local got
  # Byte-for-byte the legacy pipeline, with one addition: `tee` into a byte
  # counter standing in for the consumer, so we can measure exactly how much
  # unauthenticated plaintext sha256sum was handed. The pipeline shape — and
  # therefore the discarded exit status — is unchanged.
  got="$(gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$2" \
         -d "$1" 2>/dev/null | tee >(wc -c | tr -d ' ' > "$W/fx.released") | sha256sum | cut -d' ' -f1)"
  wait 2>/dev/null || true
  # the legacy path then hands the SAME ciphertext to downstream stages
  if [[ "$got" == "$3" ]]; then
    gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$2" \
        -d "$1" 2>/dev/null > "$W/legacy.pt" || true
    consume "$W/legacy.pt"
    return 0
  fi
  return 1
}

strict_gate(){ # <ct> <passfile> <expected_sha> <size> <fmt>
  if ntc_decrypt_validated "$1" "$2" "$3" "$4" "$5" 'set-A'; then
    consume "$NTC_VALIDATED_PATH"
    # the caller owns the validated plaintext and must release it; the
    # end-of-run emptiness assertion is what enforces this
    ntc_release_validated
    return 0
  fi
  return 1
}

# ── build the material: a real gzipped tar, so "extraction" is meaningful ───
mkdir -p "$W/payload"
head -c 400000 /dev/urandom > "$W/payload/db.bin"
head -c 200000 /dev/urandom > "$W/payload/globals.bin"
tar -czf "$W/plain.tgz" -C "$W/payload" .
PT="$W/plain.tgz"
PT_SHA="$(sha256sum "$PT" | cut -d' ' -f1)"
PT_SIZE="$(stat -c %s "$PT")"

# Encrypt with OCB when available (what the builder produces on the homelab),
# otherwise CFB+MDC. BOTH are exercised in CI terms: the format actually used
# is reported, and the expected-format binding is set to match, so this suite
# is meaningful on a 2.2.27 host and on a 2.4.4 host alike.
if gpg --batch --yes --quiet --no-tty --pinentry-mode loopback --passphrase-file "$KEY" \
      --symmetric --cipher-algo AES256 --force-ocb -o "$W/ct.gpg" "$PT" 2>/dev/null; then
  FMT=ocb
else
  gpg --batch --yes --quiet --no-tty --pinentry-mode loopback --passphrase-file "$KEY" \
      --symmetric --cipher-algo AES256 -o "$W/ct.gpg" "$PT" 2>/dev/null
  FMT=mdc
fi
CT="$W/ct.gpg"; CT_SIZE="$(stat -c %s "$CT")"

echo "C-1 authenticated decryption   impl=$IMPL  gpg=$(gpg --version | head -1 | awk '{print $3}')  format=$FMT"
echo "payload=$PT_SIZE bytes  ciphertext=$CT_SIZE bytes"
echo

# ── control: the intact component MUST be accepted, and MUST consume ────────
reset_effects
if [[ "$IMPL" == legacy ]]; then legacy_gate "$CT" "$KEY" "$PT_SHA"; g=$?
else strict_gate "$CT" "$KEY" "$PT_SHA" "$PT_SIZE" "$FMT"; g=$?; fi
if [[ $g -eq 0 && -e "$W/fx.commit" ]]; then
  OK=$((OK+1)); note ok "POSITIVE: intact component accepted and consumed"
else
  BAD=$((BAD+1)); note NOT-OK "POSITIVE: intact component accepted and consumed" "gate=$g"
fi

# ── negative controls ───────────────────────────────────────────────────────
# each: <label> <mutator> <expected strict failure class>
mutate(){ # <name> -> writes $W/m.gpg
  cp "$CT" "$W/m.gpg"
  local S; S="$(stat -c %s "$W/m.gpg")"
  case "$1" in
    tagflip)   printf '\xff' | dd of="$W/m.gpg" bs=1 seek=$((S-8))  conv=notrunc status=none ;;
    tagflip2)  printf '\xff' | dd of="$W/m.gpg" bs=1 seek=$((S-2))  conv=notrunc status=none ;;
    bodyflip)  printf '\xff' | dd of="$W/m.gpg" bs=1 seek=5000      conv=notrunc status=none ;;
    truncate)  head -c $((S-40)) "$CT" > "$W/m.gpg" ;;
    append)    cat "$CT" > "$W/m.gpg"; head -c 64 /dev/urandom >> "$W/m.gpg" ;;
    header)    printf '\x00\x00\x00\x00' | dd of="$W/m.gpg" bs=1 seek=0 conv=notrunc status=none ;;
    onebyte)   printf '\x8c' > "$W/m.gpg" ;;
  esac
}

run_negative(){ # <label> <mutation|wrongkey|replay> <expected_class>
  local label="$1" kind="$2" want="$3" ct="$W/m.gpg" key="$KEY" g
  reset_effects; NTC_ERR=''
  case "$kind" in
    wrongkey) cp "$CT" "$W/m.gpg"; key="$KEY2" ;;
    replay)   # A DIFFERENT valid component from another set, same key, and
              # deliberately the SAME PLAINTEXT SIZE. If the replay payload
              # were a different size the cheap size check would reject it and
              # the control would never exercise the digest binding at all —
              # it would pass for the wrong reason, which is the C-3 defect.
              head -c "$PT_SIZE" /dev/urandom > "$W/other.bin"
              if [[ $FMT == ocb ]]; then
                gpg --batch --yes --quiet --no-tty --pinentry-mode loopback --passphrase-file "$KEY" \
                    --symmetric --cipher-algo AES256 --force-ocb -o "$W/m.gpg" "$W/other.bin" 2>/dev/null
              else
                gpg --batch --yes --quiet --no-tty --pinentry-mode loopback --passphrase-file "$KEY" \
                    --symmetric --cipher-algo AES256 -o "$W/m.gpg" "$W/other.bin" 2>/dev/null
              fi ;;
    *)        mutate "$kind" ;;
  esac

  if [[ "$IMPL" == legacy ]]; then legacy_gate "$ct" "$key" "$PT_SHA"; g=$?
  else strict_gate "$ct" "$key" "$PT_SHA" "$PT_SIZE" "$FMT"; g=$?; fi

  local leaked rel; leaked="$(effects_seen | tr '\n' ',' | sed 's/,$//')"; rel="$(released_bytes)"

  if [[ $g -eq 0 ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "*** ACCEPTED corruption; downstream ran: ${leaked:-none} ***"
  elif [[ "$rel" -gt 0 ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "*** rejected, but $rel bytes of UNAUTHENTICATED plaintext were released to a consumer ***"
  elif [[ -n "$leaked" ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "rejected but plaintext already reached: $leaked"
  elif [[ "$IMPL" == strict && -n "$want" && "$NTC_ERR" != "$want" ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "rejected for the WRONG reason: got '$NTC_ERR' want '$want'"
  else
    OK=$((OK+1)); note ok "$label" "$([[ "$IMPL" == strict ]] && echo "class=$NTC_ERR" || echo rejected)"
  fi
}

run_negative "ciphertext mutation (body, offset 5000)"   bodyflip  gpg_status
run_negative "authentication-tag-only mutation (size-8)" tagflip   gpg_status
run_negative "authentication-tag-only mutation (size-2)" tagflip2  gpg_status
run_negative "truncation (last 40 bytes removed)"        truncate  gpg_status
run_negative "appended bytes (64 random)"                append    gpg_status
run_negative "wrong key"                                 wrongkey  gpg_status
run_negative "malformed header"                          header    malformed_header
run_negative "one-byte file"                             onebyte   malformed_header
run_negative "cross-set replay (valid, wrong component)" replay    digest_mismatch

# ── the quarantine must leave nothing behind ────────────────────────────────
resid="$(find "$NTC_QUARANTINE_DIR" -type f 2>/dev/null | wc -l)"
if [[ "$resid" -eq 0 ]]; then
  OK=$((OK+1)); note ok "quarantine directory is empty after all controls"
else
  BAD=$((BAD+1)); note NOT-OK "quarantine directory is empty after all controls" "$resid file(s) left"
fi

echo
echo "C-1: $OK ok, $BAD not-ok"
if [[ "$IMPL" == legacy ]]; then
  if [[ $BAD -gt 0 ]]; then echo "C-1 SUITE RED (impl=legacy) — as required before the fix"; exit 0
  else echo "C-1 SUITE UNEXPECTEDLY GREEN UNDER LEGACY — the reproduction has stopped reproducing"; exit 1; fi
else
  if [[ $BAD -eq 0 ]]; then echo "C-1 SUITE GREEN (impl=strict)"; exit 0
  else echo "C-1 SUITE RED (impl=strict) — the fix does not hold"; exit 1; fi
fi
