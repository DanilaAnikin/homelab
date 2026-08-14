#!/usr/bin/env bash
# ============================================================================
# C-4 — archive provenance and completeness, red-before-fix.
#
#   NT_IMPL=legacy  reproduces nt-recovery-set.sh:167-181: docker cp with no
#                   digest round-trip, `[[ -s ]]`, and `pg_restore -l | grep`
#                   as the completeness gate. MUST be red.
#   NT_IMPL=strict  uses ntc_archive_roundtrip + ntc_archive_fully_consumable.
#                   MUST be green.
#
# THE CLAIM BEING DESTROYED
# -------------------------
# `pg_restore -l` reads the header and the table of contents, both of which sit
# at the FRONT of a custom-format archive. It never touches the data blocks. So
# an archive truncated anywhere after its TOC still lists a complete inventory,
# still satisfies `grep 'TABLE DATA vault secrets'`, still passes `[[ -s ]]`,
# and still gets encrypted, uploaded and published as COMPLETE.
#
# A TOC entry proves the archive DESCRIBES an item. It proves nothing whatever
# about that item's data bytes. The only honest test is full consumption, and
# the independent oracle is a strict restore into a real server.
#
# Runs entirely against the exact production image. No production access.
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/nt-crypto.sh
. "$HERE/../lib/nt-crypto.sh"

IMPL="${NT_IMPL:-strict}"
IMAGE="${NT_TEST_IMAGE:-supabase/postgres:17.6.1.136}"
RUN="c4-$$-$RANDOM"
CN="ntc4-$RUN"
W="$(mktemp -d "${TMPDIR:-/tmp}/c4test.XXXXXX")"
chmod 700 "$W"

OK=0; BAD=0
note(){ printf '  %-6s %-54s %s\n' "$1" "$2" "${3:-}"; }
cleanup(){ docker rm -f "$CN" >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT INT TERM

echo "C-4 archive provenance   impl=$IMPL  image=$IMAGE"

# ── a real server with real Vault rows ──────────────────────────────────────
docker run -d --name "$CN" --network none \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" "$IMAGE" >/dev/null

X(){ docker exec "$CN" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -Atq -c "$1"; }

# SEMANTIC readiness, not pg_isready. The supabase image bootstraps in stages
# and restarts the server partway through, so pg_isready reports "accepting
# connections" during a window in which every query still fails with
# "the database system is starting up". Requiring N CONSECUTIVE successful
# round trips as the role we will actually use is what makes this stable —
# a single success can land inside that window.
ready=0
for _ in $(seq 1 240); do
  if X 'SELECT 1' >/dev/null 2>&1; then
    ready=$((ready+1)); [[ $ready -ge 5 ]] && break
  else
    ready=0
  fi
  sleep 1
done
[[ $ready -ge 5 ]] || { echo "FATAL: server never became semantically ready"; docker logs --tail 40 "$CN"; exit 1; }

# Populate enough real data that the archive has substantial data blocks AFTER
# the TOC — the truncation has to have something to remove.
X "CREATE EXTENSION IF NOT EXISTS supabase_vault CASCADE" >/dev/null 2>&1 || true
# fatal on failure: a silently-empty fixture is how a suite ends up measuring
# nothing while reporting confidently
X "CREATE TABLE IF NOT EXISTS public.bulk(id int primary key, payload text)" >/dev/null \
  || { echo "FATAL: could not create the bulk fixture"; exit 1; }
X "INSERT INTO public.bulk SELECT g, repeat(md5(g::text), 40) FROM generate_series(1,60000) g ON CONFLICT DO NOTHING" >/dev/null \
  || { echo "FATAL: could not populate the bulk fixture"; exit 1; }
VS_BEFORE="$(X "SELECT count(*) FROM vault.secrets" 2>/dev/null || echo 0)"
if [[ "${VS_BEFORE:-0}" -eq 0 ]]; then
  X "SELECT vault.create_secret('drill-secret-one','nt-c4-a')" >/dev/null 2>&1 || true
  X "SELECT vault.create_secret('drill-secret-two','nt-c4-b')" >/dev/null 2>&1 || true
fi
VAULT_N="$(X "SELECT count(*) FROM vault.secrets")"
BULK_N="$(X "SELECT count(*) FROM public.bulk")"
echo "source: vault.secrets=$VAULT_N  public.bulk=$BULK_N"
[[ "${BULK_N:-0}" -ge 60000 ]] || { echo "FATAL: bulk fixture is $BULK_N rows, expected 60000"; exit 1; }
[[ "${VAULT_N:-0}" -ge 1 ]]    || { echo "FATAL: no vault rows, the TOC claim cannot be tested"; exit 1; }

docker exec "$CN" pg_dump -U supabase_admin -Fc -Z6 -f /tmp/good.dump postgres 2>/dev/null || {
  echo "FATAL: pg_dump failed"; exit 1; }
GOOD_SIZE="$(docker exec "$CN" stat -c %s /tmp/good.dump)"
docker cp "$CN:/tmp/good.dump" "$W/good.dump" >/dev/null
echo "archive: $GOOD_SIZE bytes"
echo

# ── where does the TOC end? ─────────────────────────────────────────────────
# Determined empirically rather than assumed: the largest prefix that
# `pg_restore -l` still reads cleanly is at or past the end of the TOC.
TOC_END=0
# ascending prefix sizes, so the FIRST success really is the smallest prefix
# whose TOC still reads — the sharpest possible statement of the defect
for frac in 512 256 128 64 32 16 8 4 2; do
  n=$(( GOOD_SIZE / frac ))
  head -c "$n" "$W/good.dump" > "$W/probe.dump"
  if docker run --rm --network none -v "$W:/w:ro" "$IMAGE" pg_restore -l /w/probe.dump >/dev/null 2>&1; then
    TOC_END="$n"; break
  fi
done
[[ "$TOC_END" -gt 0 ]] || { echo "FATAL: could not locate a post-TOC truncation point"; exit 1; }
echo "smallest prefix whose TOC still lists cleanly: $TOC_END bytes ($(( TOC_END * 100 / GOOD_SIZE ))% of the archive)"
echo

# ── the two implementations ─────────────────────────────────────────────────
# legacy: nt-recovery-set.sh:170-181, verbatim in behaviour
legacy_gate(){ # <host_archive>
  [[ -s "$1" ]] || return 1
  docker run --rm -i --network none -v "$W:/w:ro" "$IMAGE" \
    pg_restore -l "/w/$(basename "$1")" > "$W/toc.txt" 2>/dev/null || return 1
  grep -q 'TABLE DATA vault secrets' "$W/toc.txt" || return 1
  return 0
}
strict_gate(){ # <host_archive>
  ntc_archive_declares "$IMAGE" "$1" 'TABLE DATA vault secrets' || return 1
  ntc_archive_fully_consumable "$IMAGE" "$1" || return 1
  return 0
}
gate(){ if [[ "$IMPL" == legacy ]]; then legacy_gate "$1"; else strict_gate "$1"; fi; }

# ── the independent oracle: a strict restore into a real server ─────────────
oracle_restores(){ # <host_archive>  -> 0 if a strict restore succeeds
  local a="$1" db="orc$RANDOM"
  docker cp "$a" "$CN:/tmp/orc.dump" >/dev/null 2>&1 || return 1
  docker exec "$CN" psql -U supabase_admin -d postgres -Atqc "CREATE DATABASE $db OWNER supabase_admin" >/dev/null 2>&1 || return 1
  local rc=0
  docker exec "$CN" pg_restore -U supabase_admin -d "$db" --exit-on-error /tmp/orc.dump >/dev/null 2>&1 || rc=$?
  docker exec "$CN" psql -U supabase_admin -d postgres -Atqc "DROP DATABASE $db" >/dev/null 2>&1 || true
  return $rc
}

control(){ # <label> <archive> <expect: accept|reject> [expected_strict_class]
  local label="$1" arch="$2" expect="$3" want="${4:-}" g o
  NTC_ERR=''
  gate "$arch"; g=$?
  oracle_restores "$arch"; o=$?

  if [[ "$expect" == accept ]]; then
    if [[ $g -eq 0 && $o -eq 0 ]]; then OK=$((OK+1)); note ok "$label"
    else BAD=$((BAD+1)); note NOT-OK "$label" "gate=$g oracle=$o (both should be 0)"; fi
    return
  fi

  # expect reject: the oracle must agree the archive is unusable, otherwise the
  # control is not testing what it claims
  if [[ $o -eq 0 ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "oracle RESTORED it — the control's premise is false"; return
  fi
  if [[ $g -eq 0 ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "*** GATE ACCEPTED an archive the oracle cannot restore ***"
  elif [[ "$IMPL" == strict && -n "$want" && "$NTC_ERR" != "$want" ]]; then
    BAD=$((BAD+1)); note NOT-OK "$label" "rejected for the WRONG reason: got '$NTC_ERR' want '$want'"
  else
    OK=$((OK+1)); note ok "$label" "$([[ "$IMPL" == strict ]] && echo "class=$NTC_ERR" || echo rejected)"
  fi
}

control "POSITIVE: intact archive" "$W/good.dump" accept

head -c "$TOC_END" "$W/good.dump" > "$W/t_toc.dump"
control "truncated immediately after the TOC" "$W/t_toc.dump" reject archive_not_consumable

head -c $(( GOOD_SIZE * 3 / 4 )) "$W/good.dump" > "$W/t_mid.dump"
control "truncated inside table data (75%)" "$W/t_mid.dump" reject archive_not_consumable

head -c $(( GOOD_SIZE - 64 )) "$W/good.dump" > "$W/t_tail.dump"
control "missing the final 64 bytes" "$W/t_tail.dump" reject archive_not_consumable

printf '\x50' > "$W/t_one.dump"
control "one-byte output" "$W/t_one.dump" reject toc_unreadable

cp "$W/good.dump" "$W/t_alt.dump"
printf '\xff\xff\xff\xff' | dd of="$W/t_alt.dump" bs=1 seek=$(( GOOD_SIZE / 2 )) conv=notrunc status=none
control "altered host copy (4 bytes flipped mid-archive)" "$W/t_alt.dump" reject archive_not_consumable

# ── the round-trip controls, which the legacy path cannot express at all ────
echo
echo "  -- container->host round trip (legacy has no equivalent; strict only) --"
if [[ "$IMPL" == strict ]]; then
  NTC_ERR=''
  if ntc_archive_roundtrip "$CN" /tmp/good.dump "$W/rt.dump"; then
    OK=$((OK+1)); note ok "round trip of the intact archive" "sha=${NTC_ARCHIVE_SHA:0:16}… size=$NTC_ARCHIVE_SIZE"
  else
    BAD=$((BAD+1)); note NOT-OK "round trip of the intact archive" "class=$NTC_ERR"
  fi

  # host copy altered after the fact must be caught by digest, not by size
  NTC_ERR=''
  docker exec "$CN" sh -c 'cp /tmp/good.dump /tmp/same.dump && printf "\xff" | dd of=/tmp/same.dump bs=1 seek=100 conv=notrunc status=none' >/dev/null 2>&1
  if ntc_archive_roundtrip "$CN" /tmp/same.dump "$W/rt2.dump"; then
    # same size, different content: the round trip itself is honest (both sides
    # match) — this proves the digest is computed at the SOURCE, not re-derived
    OK=$((OK+1)); note ok "same-size mutation still round-trips (digest is source-computed)"
  else
    BAD=$((BAD+1)); note NOT-OK "same-size mutation still round-trips" "class=$NTC_ERR"
  fi

  NTC_ERR=''
  if ntc_archive_roundtrip "$CN" /tmp/does-not-exist.dump "$W/rt3.dump"; then
    BAD=$((BAD+1)); note NOT-OK "absent in-container archive is rejected" "accepted"
  elif [[ "$NTC_ERR" != container_digest_failed ]]; then
    BAD=$((BAD+1)); note NOT-OK "absent in-container archive is rejected" "wrong class: $NTC_ERR"
  else
    OK=$((OK+1)); note ok "absent in-container archive is rejected" "class=$NTC_ERR"
  fi

  NTC_ERR=''
  docker exec "$CN" sh -c 'printf x > /tmp/tiny.dump' >/dev/null 2>&1
  if ntc_archive_roundtrip "$CN" /tmp/tiny.dump "$W/rt4.dump"; then
    BAD=$((BAD+1)); note NOT-OK "one-byte in-container archive is rejected" "accepted — [[ -s ]] would too"
  elif [[ "$NTC_ERR" != archive_implausibly_small ]]; then
    BAD=$((BAD+1)); note NOT-OK "one-byte in-container archive is rejected" "wrong class: $NTC_ERR"
  else
    OK=$((OK+1)); note ok "one-byte in-container archive is rejected" "class=$NTC_ERR"
  fi

  NTC_ERR=''
  if ntc_archive_roundtrip "ntc4-nonexistent-$RANDOM" /tmp/good.dump "$W/rt5.dump"; then
    BAD=$((BAD+1)); note NOT-OK "source container removed before comparison" "accepted"
  elif [[ "$NTC_ERR" != container_digest_failed ]]; then
    BAD=$((BAD+1)); note NOT-OK "source container removed before comparison" "wrong class: $NTC_ERR"
  else
    OK=$((OK+1)); note ok "source container removed before comparison" "class=$NTC_ERR"
  fi
fi

# ── the headline: TOC listing succeeds while consumption fails ──────────────
echo
echo "  -- the specific claim the audit made --"
TOCLIST=no; grep -q 'TABLE DATA vault secrets' <(docker run --rm --network none -v "$W:/w:ro" "$IMAGE" \
  pg_restore -l /w/t_toc.dump 2>/dev/null) && TOCLIST=yes
CONSUME=no; docker run --rm --network none -v "$W:/w:ro" "$IMAGE" \
  pg_restore -f /dev/null /w/t_toc.dump >/dev/null 2>&1 && CONSUME=yes
note INFO "post-TOC-truncated archive: 'pg_restore -l' lists vault TABLE DATA" "$TOCLIST"
note INFO "post-TOC-truncated archive: 'pg_restore -f /dev/null' consumes it" "$CONSUME"
if [[ "$TOCLIST" == yes && "$CONSUME" == no ]]; then
  OK=$((OK+1)); note ok "a TOC entry does not imply the data bytes are present"
else
  BAD=$((BAD+1)); note NOT-OK "a TOC entry does not imply the data bytes are present" "toc=$TOCLIST consume=$CONSUME"
fi

echo
echo "C-4: $OK ok, $BAD not-ok"
if [[ "$IMPL" == legacy ]]; then
  if [[ $BAD -gt 0 ]]; then echo "C-4 SUITE RED (impl=legacy) — as required before the fix"; exit 0
  else echo "C-4 SUITE UNEXPECTEDLY GREEN UNDER LEGACY"; exit 1; fi
else
  if [[ $BAD -eq 0 ]]; then echo "C-4 SUITE GREEN (impl=strict)"; exit 0
  else echo "C-4 SUITE RED (impl=strict) — the fix does not hold"; exit 1; fi
fi
