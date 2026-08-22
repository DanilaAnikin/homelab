#!/usr/bin/env bash
# ============================================================================
# nt-b4-seal-artifact.sh — build, describe, scan and seal the frozen bridge as
# an offline content-addressed artifact.
#
# WHAT "CONTENT-ADDRESSED" MEANS HERE, AND WHAT IT DOES NOT
# ---------------------------------------------------------
# Everything this produces is addressed by a digest computed LOCALLY, from
# bytes on this disk. That is a real and useful property: it lets a transfer be
# verified, an import be compared against its export, and a deployment be
# pinned to something that cannot change under it.
#
# It is NOT a registry digest. Nothing here is pushed, nothing is fetched by
# digest from a registry, and no registry has attested to any of it. A local
# `docker images --digests` column is empty for a locally built image precisely
# because that field means "what a registry said", and no registry has said
# anything. Nor is any of this a signed registry artifact: cosign is used, if
# at all, over a local file, and a signature over a file you built yourself
# attests to custody, not to provenance.
#
# The distinction matters because the two are routinely conflated in exactly
# the situation where it is most misleading — an air-gapped transfer, where the
# only thing anyone can check is the thing this script computes.
#
# WHAT IT PRODUCES
#   image.tar                the exported image
#   image.tar.sha256         its digest, computed before transfer
#   manifest.json            OCI config + layer digests, read from the image
#   sbom.spdx.json           SBOM (syft)
#   sbom.cyclonedx.json      SBOM (syft, second format — different consumers)
#   vulns.json               vulnerability scan (trivy)
#   layers/                  per-layer secret scan results
#   provenance.json          how this was built, from what, with which tools
#   SEALED.txt               the human-readable summary and every digest
#
# Usage:
#   nt-b4-seal-artifact.sh --source-sha <40-char> [--out <dir>]
#   nt-b4-seal-artifact.sh --verify <dir>          re-check a sealed directory
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

REPO="${NT_REPO:-/home/anakin/programming/nate_trader}"
SYFT_IMAGE="${NT_SYFT_IMAGE:-anchore/syft:latest}"
TRIVY_IMAGE="${NT_TRIVY_IMAGE:-aquasec/trivy:latest}"
SMOKE_PORT="${NT_SMOKE_PORT:-127.0.0.1:18310}"
# The internal Kong origin. Deliberately the unique container name: six tenants
# share dokploy-network and every one of their gateways answers to the alias
# `kong`, so the short name round-robins between other people's gateways.
INTERNAL_URL="${NT_INTERNAL_SUPABASE_URL:-http://natetrader-supabase-kong:8000}"

# Build arguments. NEXT_PUBLIC_* values are INLINED INTO THE BROWSER BUNDLE at
# build time, so they are part of the artifact's identity, not of its runtime
# configuration: the same source built with a different URL is a different
# artifact. The Dockerfile refuses to build without them, which is correct and
# is how the first attempt at this script failed.
#
# The anon key is public by construction — it ships in the bundle every visitor
# downloads — but it is read from a mode-0600 file rather than passed on a
# command line, because an argument is visible in the process table to every
# user on the box, and "public eventually" is not "public to your neighbours
# right now". It is never echoed.
PUBLIC_URL="${NT_PUBLIC_SUPABASE_URL:-https://ntapi.anikin.cz}"
COOKIE_NAME="${NT_AUTH_COOKIE_NAME:-sb-ntapi-auth-token}"
ANON_KEY_FILE="${NT_ANON_KEY_FILE:-$HOME/.config/nate-trader/anon-key.txt}"

OK=0; BAD=0
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
ok(){   OK=$((OK+1));  note ok   "$1" "${2:-}"; }
bad(){  BAD=$((BAD+1)); note FAIL "$1" "${2:-}"; }
die(){  echo; echo "ABORT: $*"; exit 1; }

SOURCE_SHA=""; OUT=""; MODE=seal
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-sha) SOURCE_SHA="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    --verify)     MODE=verify; OUT="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ── verify mode: re-check a directory somebody else produced ────────────────
if [[ "$MODE" == verify ]]; then
  [[ -d "$OUT" ]] || die "$OUT is not a directory"
  echo "verifying sealed artifact in $OUT"
  echo
  ( cd "$OUT" && sha256sum -c image.tar.sha256 >/dev/null 2>&1 ) \
    && ok "image.tar matches its recorded digest" \
    || bad "image.tar does NOT match image.tar.sha256"
  for f in manifest.json sbom.spdx.json sbom.cyclonedx.json vulns.json provenance.json SEALED.txt; do
    [[ -s "$OUT/$f" ]] && ok "$f present and non-empty" || bad "$f missing or empty"
  done
  # every digest SEALED.txt claims must match the file it names
  while read -r digest name; do
    [[ -f "$OUT/$name" ]] || { bad "SEALED.txt names a missing file: $name"; continue; }
    got="$(sha256sum "$OUT/$name" | cut -d' ' -f1)"
    [[ "$got" == "$digest" ]] && ok "digest matches: $name" || bad "digest MISMATCH: $name"
  done < <(sed -n 's/^DIGEST \([0-9a-f]\{64\}\)  \(.*\)$/\1 \2/p' "$OUT/SEALED.txt")
  echo
  echo "verify: $OK ok, $BAD failed"
  [[ $BAD -eq 0 ]] || exit 1
  exit 0
fi

# ── seal mode ───────────────────────────────────────────────────────────────
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || die "--source-sha must be a full 40-character SHA (got '${SOURCE_SHA}')"
OUT="${OUT:-/var/tmp/nt-bridge-seal-${SOURCE_SHA:0:12}}"
mkdir -p "$OUT/layers"

TAG="nt-bridge:${SOURCE_SHA:0:12}"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nt-seal-src.XXXXXX")"
cleanup(){ rm -rf "$BUILD_DIR"; docker rm -f "nt-smoke-$$" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "sealing the frozen bridge"
echo "  source  $SOURCE_SHA"
echo "  out     $OUT"
echo

# ── 1. the source, at exactly that commit and nothing else ──────────────────
# `git archive` rather than a copy of a worktree: a worktree carries whatever
# is uncommitted, and an artifact built from "the commit plus some edits" is
# not an artifact built from the commit.
git -C "$REPO" archive --format=tar "$SOURCE_SHA" | tar -xf - -C "$BUILD_DIR" \
  || die "cannot archive $SOURCE_SHA — is it fetched?"
ok "source extracted from git archive" "$SOURCE_SHA"

TREE="$(git -C "$REPO" rev-parse "$SOURCE_SHA^{tree}")"
ok "source tree recorded" "$TREE"

# ── 2. build ────────────────────────────────────────────────────────────────
[[ -r "$ANON_KEY_FILE" ]] || die "no anon key at $ANON_KEY_FILE (NEXT_PUBLIC_SUPABASE_ANON_KEY is a build arg)"
ANON_KEY="$(tr -d '\r\n' < "$ANON_KEY_FILE")"
[[ -n "$ANON_KEY" ]] || die "$ANON_KEY_FILE is empty"

# The cookie name must be the one the browser actually writes. Getting this
# wrong produces a build where SSR and the proxy look for a cookie that does
# not exist, find no session, and redirect every signed-in user to /login —
# a total outage that no health check notices.
DERIVED="sb-$(printf '%s' "$PUBLIC_URL" | sed -E 's#^https?://##; s#/.*##; s#\..*##')-auth-token"
[[ "$COOKIE_NAME" == "$DERIVED" ]] \
  && ok "auth cookie name agrees with the public URL" "$COOKIE_NAME" \
  || bad "cookie name '$COOKIE_NAME' does not match what '$PUBLIC_URL' derives ('$DERIVED')"

docker build -t "$TAG" -f "$BUILD_DIR/dashboard/Dockerfile" \
  --build-arg "NEXT_PUBLIC_SUPABASE_URL=$PUBLIC_URL" \
  --build-arg "NEXT_PUBLIC_SUPABASE_ANON_KEY=$ANON_KEY" \
  --build-arg "NEXT_PUBLIC_SUPABASE_AUTH_COOKIE_NAME=$COOKIE_NAME" \
  --build-arg "BUILD_SHA=$SOURCE_SHA" \
  "$BUILD_DIR/dashboard" \
  >"$OUT/build.log" 2>&1 || { tail -20 "$OUT/build.log"; die "build failed"; }
ok "image built" "$TAG"

# The build args are the artifact's identity, so they are asserted in the
# image rather than trusted from the command that produced it.
BAKED_SHA="$(docker image inspect "$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^BUILD_SHA=//p')"
[[ "$BAKED_SHA" == "$SOURCE_SHA" ]] && ok "BUILD_SHA baked into the image" "$BAKED_SHA" \
                                    || bad "BUILD_SHA in the image is '$BAKED_SHA', not the source SHA"
BAKED_COOKIE="$(docker image inspect "$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^NEXT_PUBLIC_SUPABASE_AUTH_COOKIE_NAME=//p')"
[[ "$BAKED_COOKIE" == "$COOKIE_NAME" ]] && ok "auth cookie name carried into the runner" "$BAKED_COOKIE" \
                                        || bad "cookie name in the runner is '$BAKED_COOKIE'"

# NEXT_PUBLIC_SUPABASE_URL is deliberately NOT in the runner's environment.
# The Dockerfile sets it only in the builder, because its job there is to be
# INLINED INTO THE BROWSER BUNDLE; the server half reads it from the runtime
# environment the way production supplies it. An earlier version of this script
# asserted it in `docker image inspect` and failed the seal for the artifact
# being built correctly.
#
# So the property is checked where it actually lives: in the compiled client
# chunks. That is also the stronger check — the bundle is what every visitor
# downloads, and a bundle pointing at the wrong origin is an outage no server
# env var can fix.
INLINED="$(docker run --rm --entrypoint sh "$TAG" -c \
  "grep -rl '$PUBLIC_URL' /app/.next/static 2>/dev/null | head -3" || true)"
[[ -n "$INLINED" ]] && ok "public URL inlined into the browser bundle" "$(printf '%s' "$INLINED" | head -1)" \
                    || bad "the public URL is NOT in the built bundle" "the browser would call the wrong origin"

# The local image ID. NOT a registry digest — see the header.
IMAGE_ID="$(docker image inspect "$TAG" --format '{{.Id}}')"
ok "local image ID (not a registry digest)" "$IMAGE_ID"

docker image inspect "$TAG" --format '{{json .}}' > "$OUT/manifest.json"
docker image inspect "$TAG" --format '{{range .RootFS.Layers}}{{println .}}{{end}}' \
  | grep -v '^$' > "$OUT/layer-digests.txt"
ok "layer digests recorded" "$(wc -l < "$OUT/layer-digests.txt") layers"

# ── 3. describe: two SBOM formats ───────────────────────────────────────────
# Two because they are consumed by different tools and disagreeing SBOMs are
# a signal worth having.
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT_IMAGE" \
  "docker:$TAG" -o spdx-json > "$OUT/sbom.spdx.json" 2>"$OUT/syft.err" \
  && ok "SBOM (SPDX)" "$(wc -c < "$OUT/sbom.spdx.json") bytes" \
  || bad "syft SPDX failed" "$(tail -1 "$OUT/syft.err")"

docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT_IMAGE" \
  "docker:$TAG" -o cyclonedx-json > "$OUT/sbom.cyclonedx.json" 2>>"$OUT/syft.err" \
  && ok "SBOM (CycloneDX)" "$(wc -c < "$OUT/sbom.cyclonedx.json") bytes" \
  || bad "syft CycloneDX failed"

# An SBOM that lists nothing is not an SBOM.
PKGS="$(python3 -c "
import json,sys
try:
    d=json.load(open('$OUT/sbom.spdx.json'))
    print(len(d.get('packages',[])))
except Exception:
    print(0)
")"
[[ "$PKGS" -gt 10 ]] && ok "SBOM is non-empty" "$PKGS packages" \
                     || bad "SBOM lists $PKGS packages — that is not a description of anything"

# ── 4. scan ─────────────────────────────────────────────────────────────────
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.cache/trivy:/root/.cache/trivy" "$TRIVY_IMAGE" \
  image --quiet --format json "$TAG" > "$OUT/vulns.json" 2>"$OUT/trivy.err" \
  && ok "vulnerability scan complete" \
  || bad "trivy failed" "$(tail -1 "$OUT/trivy.err")"

python3 - "$OUT/vulns.json" > "$OUT/vuln-summary.txt" <<'PY'
import json,sys,collections
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print("UNREADABLE"); raise SystemExit
c=collections.Counter()
for r in d.get("Results") or []:
    for v in r.get("Vulnerabilities") or []:
        c[v.get("Severity","UNKNOWN")]+=1
for sev in ("CRITICAL","HIGH","MEDIUM","LOW","UNKNOWN"):
    print(f"{sev} {c[sev]}")
PY
CRIT="$(awk '$1=="CRITICAL"{print $2}' "$OUT/vuln-summary.txt")"
HIGH="$(awk '$1=="HIGH"{print $2}' "$OUT/vuln-summary.txt")"
# GATED. A seal is a statement that this artifact is fit to deploy; printing
# "SEAL GREEN" over a CRITICAL finding, as this used to ("recorded, not gated
# here"), is the seal asserting something it did not check. The threshold is a
# named default so an operator can seal a known-accepted image deliberately
# (NT_SEAL_MAX_SEVERITY=high|critical|none) rather than by the tool looking away.
SEAL_MAX="${NT_SEAL_MAX_SEVERITY:-high}"   # default: no CRITICAL and no HIGH
case "$SEAL_MAX" in
  none)     [[ "${CRIT:-0}" -eq 0 && "${HIGH:-0}" -eq 0 ]] && ok "no CRITICAL or HIGH vulnerabilities" "CRITICAL=$CRIT HIGH=$HIGH"                                                           || bad "vulnerabilities present" "CRITICAL=$CRIT HIGH=$HIGH (NT_SEAL_MAX_SEVERITY=none)" ;;
  high)     [[ "${CRIT:-0}" -eq 0 && "${HIGH:-0}" -eq 0 ]] && ok "no CRITICAL or HIGH vulnerabilities" "CRITICAL=$CRIT HIGH=$HIGH"                                                           || bad "CRITICAL or HIGH vulnerabilities present" "CRITICAL=$CRIT HIGH=$HIGH — raise NT_SEAL_MAX_SEVERITY to accept them deliberately" ;;
  critical) [[ "${CRIT:-0}" -eq 0 ]] && ok "no CRITICAL vulnerabilities" "CRITICAL=$CRIT (HIGH=$HIGH accepted by NT_SEAL_MAX_SEVERITY=critical)"                                      || bad "CRITICAL vulnerabilities present" "CRITICAL=$CRIT" ;;
  *) bad "NT_SEAL_MAX_SEVERITY invalid" "got '$SEAL_MAX', want none|high|critical" ;;
esac

# ── 4b. scan-scope caveat ───────────────────────────────────────────────────
# trivy can only match a CVE to a package that HAS a version. syft records many
# of Next's vendored dependencies (under next/dist/compiled/) with no version at
# all, so those components are outside the scan the "0 findings" above reports —
# including Next's own bundled copies of packages that DID carry CVEs elsewhere.
# Reporting the severity block without this is the seal claiming clean over a
# surface it did not fully see (audit F2). Computed from the SBOM, not hardcoded.
python3 - "$OUT/sbom.cyclonedx.json" > "$OUT/scan-scope.txt" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print("UNREADABLE"); raise SystemExit
comps=d.get("components",[])
def nover(c): return c.get("version","UNKNOWN") in ("", "UNKNOWN", None)
npm=[c for c in comps if str(c.get("purl","")).startswith("pkg:npm")]
tot, tot_nv = len(comps), sum(1 for c in comps if nover(c))
npm_n, npm_nv = len(npm), sum(1 for c in npm if nover(c))
print(f"components {tot}")
print(f"components_no_version {tot_nv}")
print(f"npm {npm_n}")
print(f"npm_no_version {npm_nv}")
PY
SCOPE_TOT="$(awk '$1=="components"{print $2}' "$OUT/scan-scope.txt")"
SCOPE_TOT_NV="$(awk '$1=="components_no_version"{print $2}' "$OUT/scan-scope.txt")"
SCOPE_NPM="$(awk '$1=="npm"{print $2}' "$OUT/scan-scope.txt")"
SCOPE_NPM_NV="$(awk '$1=="npm_no_version"{print $2}' "$OUT/scan-scope.txt")"
if [[ -n "${SCOPE_TOT_NV:-}" && "${SCOPE_TOT_NV:-0}" -gt 0 ]]; then
  ok "scan scope recorded" "$SCOPE_TOT_NV/$SCOPE_TOT components ($SCOPE_NPM_NV/$SCOPE_NPM npm) unversioned — outside the CVE scan"
else
  ok "scan scope recorded" "every SBOM component carries a version"
fi

# ── 5. per-layer secret scan ────────────────────────────────────────────────
# Filesystem-level, from an exported container rather than from the layer
# blobs: an earlier attempt scanned the blobs directly, extracted zero files,
# and reported every secret ABSENT. Zero files examined is not a clean result.
CID="$(docker create "$TAG")"
docker export "$CID" > "$OUT/layers/rootfs.tar"
docker rm -f "$CID" >/dev/null
FILES="$(tar -tf "$OUT/layers/rootfs.tar" | wc -l)"
[[ "$FILES" -gt 100 ]] && ok "rootfs exported for scanning" "$FILES entries" \
                       || bad "rootfs export produced $FILES entries — nothing was examined"

# Positive control first: prove the scanner finds a planted string, or its
# silence about real secrets means nothing.
mkdir -p "$OUT/layers/x"
echo "SUPABASE_SERVICE_ROLE_KEY=planted-positive-control" > "$OUT/layers/x/control.txt"
if grep -rlq 'SERVICE_ROLE_KEY' "$OUT/layers/x/" 2>/dev/null; then
  ok "secret scanner positive control fires"
else
  bad "secret scanner cannot find a planted secret — every ABSENT below is meaningless"
fi
rm -rf "$OUT/layers/x"

# VALUES, not identifiers.
#
# The first version matched the strings SUPABASE_SERVICE_ROLE_KEY,
# APCA-API-SECRET-KEY and -----BEGIN PRIVATE KEY----- and reported the image as
# carrying secrets. All three were names, not secrets: the first is an env var
# the server code reads, the second is an Alpaca HTTP HEADER name, the third
# sits in a dependency's test fixture. A scanner that cannot tell a variable
# name from its value cries wolf, and a scanner that cries wolf gets switched
# off — which is worse than not having one.
#
# Two things also have to be true for the verdict to mean anything: the anon
# key IS expected in the bundle (it is public and inlined on purpose), and the
# service-role key must not be, even though both are JWTs signed by the same
# issuer. Telling them apart requires reading the payload, not matching a shape.
tar -xf "$OUT/layers/rootfs.tar" -O 2>/dev/null \
  | grep -aoE 'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' \
  | sort -u > "$OUT/layers/jwts.txt" || true
tar -xf "$OUT/layers/rootfs.tar" -O 2>/dev/null \
  | grep -aozE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----[[:space:]]+[A-Za-z0-9+/=[:space:]]{200,}-----END' \
  | tr -d '\0' > "$OUT/layers/pemblocks.txt" || true

SECRET_HITS="$(python3 - "$OUT/layers/jwts.txt" "$ANON_KEY_FILE" <<'PYX'
import base64, json, sys, pathlib
anon = pathlib.Path(sys.argv[2]).read_text().strip()
def payload(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    tok = line.strip()
    if not tok or tok == anon:
        continue
    role = payload(tok).get("role", "")
    if role and role != "anon":
        print(f"JWT with role={role} present in the image")
    elif not role:
        print("unrecognised JWT present in the image")
PYX
)"
if [[ -s "$OUT/layers/pemblocks.txt" ]]; then
  SECRET_HITS="${SECRET_HITS}"$'\n'"a PEM private key block with a real body is present"
fi

ANON_PRESENT=0
grep -qxF "$(cat "$ANON_KEY_FILE" | tr -d '\r\n')" "$OUT/layers/jwts.txt" 2>/dev/null && ANON_PRESENT=1
# Positive control on the JWT extractor itself: the anon key is inlined on
# purpose, so if the extractor found NO jwt at all it is not reading the image.
[[ "$ANON_PRESENT" == 1 ]] \
  && ok "JWT extractor works" "the public anon key is present, as expected" \
  || bad "the anon key was not found in the image" "the extractor found nothing — its silence is meaningless"

if [[ -z "${SECRET_HITS//[[:space:]]/}" ]]; then
  ok "no service-role JWT and no private key in the rootfs"
else
  bad "SECRET MATERIAL IN THE IMAGE" "$(printf '%s' "$SECRET_HITS" | tr '\n' ';' | head -c 120)"
fi
printf '%s\n' "${SECRET_HITS:-none}" > "$OUT/layers/secret-scan.txt"
rm -f "$OUT/layers/rootfs.tar"   # large, and its findings are recorded

# ── 6. export, checksum, transfer, import, round-trip ───────────────────────
docker save "$TAG" -o "$OUT/image.tar"
( cd "$OUT" && sha256sum image.tar > image.tar.sha256 )
ok "exported and checksummed" "$(du -h "$OUT/image.tar" | cut -f1)"

# "Transfer" is a copy to a staging path and back. It is not a network hop,
# and calling it one would be the same overclaim this whole script is careful
# about — what it proves is that the bytes survive being moved and that the
# checksum is the thing that tells you so.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nt-seal-stage.XXXXXX")"
cp "$OUT/image.tar" "$OUT/image.tar.sha256" "$STAGE/"
( cd "$STAGE" && sha256sum -c image.tar.sha256 >/dev/null ) \
  && ok "checksum verifies after transfer" \
  || bad "checksum FAILED after transfer"

# Import under a different tag so the comparison is against a genuinely
# reloaded image rather than the one already in the daemon.
docker rmi -f "$TAG" >/dev/null 2>&1 || true
docker load -i "$STAGE/image.tar" >/dev/null
RELOADED_ID="$(docker image inspect "$TAG" --format '{{.Id}}')"
[[ "$RELOADED_ID" == "$IMAGE_ID" ]] \
  && ok "round-trip: reloaded image ID is identical" "$RELOADED_ID" \
  || bad "round-trip: image ID changed" "$IMAGE_ID -> $RELOADED_ID"

docker image inspect "$TAG" --format '{{range .RootFS.Layers}}{{println .}}{{end}}' \
  | grep -v '^$' > "$STAGE/layers-after.txt"
diff -q "$OUT/layer-digests.txt" "$STAGE/layers-after.txt" >/dev/null \
  && ok "round-trip: every layer digest is identical" \
  || bad "round-trip: layer digests changed"
rm -rf "$STAGE"

# ── 7. private smoke, on loopback only ──────────────────────────────────────
# Published to 127.0.0.1 explicitly. `-p 3000:3000` would bind 0.0.0.0 and
# publish a pre-cutover artifact to the network, which is the one thing a
# containment rehearsal must not do.
# The SAME values the bundle was built with, supplied the way production
# supplies them.
#
# The Dockerfile deliberately keeps these out of the runner image — they are
# build-time for the browser half and runtime for the server half — so a
# container started without them fails closed: getSupabaseServerUrl() throws and
# the proxy answers 503 "authentication is not configured". An earlier version
# of this script started the container bare and spent its whole readiness budget
# watching a correctly-failing image refuse to serve.
#
# Passing the same values keeps the two halves agreeing. Passing different ones
# would smoke-test a configuration the bundle does not share, which is the
# split-brain the cookie-name check above also guards against.
docker run -d --name "nt-smoke-$$" -p "${SMOKE_PORT}:3000" \
  -e "NEXT_PUBLIC_SUPABASE_URL=$PUBLIC_URL" \
  -e "NEXT_PUBLIC_SUPABASE_ANON_KEY=$ANON_KEY" \
  -e "SUPABASE_SERVER_URL=$INTERNAL_URL" \
  "$TAG" >/dev/null

BOUND="$(docker port "nt-smoke-$$" 3000 2>/dev/null || echo "")"
[[ "$BOUND" == 127.0.0.1:* ]] && ok "smoke container is loopback-only" "$BOUND" \
                              || bad "smoke container is NOT loopback-only" "$BOUND"

# READINESS IS /login, NOT /api/health.
#
# /api/health reports the whole server configuration, and a loopback smoke has
# no database, no service-role key and no GitHub token — so it correctly
# answers 503 {"status":"misconfigured"}. Waiting for a 200 there means waiting
# for something that must never happen in an isolated smoke, and an earlier
# version of this script spent its entire readiness budget doing exactly that
# before declaring the image broken.
#
# What a private smoke CAN establish is bounded and worth stating plainly: the
# image boots, serves, declares what it is, and refuses every mutating method.
# It cannot establish that data paths work, and claiming otherwise from a
# container with nothing behind it would be the overclaim this whole exercise
# is careful about.
READY=0
for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "http://${SMOKE_PORT}/login" 2>/dev/null || echo 000)"
  [[ "$code" == "200" ]] && { READY=1; break; }
  sleep 1
done
if [[ "$READY" == 1 ]]; then
  ok "smoke: the app serves" "/login -> 200"
  BODY="$(curl -sS --max-time 5 "http://${SMOKE_PORT}/api/health" || true)"
  # the identity fields are present whatever the configuration status
  [[ "$BODY" == *'"artifact_role":"frozen-containment-bridge"'* ]] \
    && ok "smoke: declares the frozen identity" || bad "smoke: wrong artifact_role"
  [[ "$BODY" == *'"writes_enabled":false'* ]] \
    && ok "smoke: writes_enabled=false" || bad "smoke: writes_enabled is not false"
  [[ "$BODY" == *'"status":"misconfigured"'* ]] \
    && ok "smoke: reports misconfigured, as an isolated container should" \
    || note info "smoke: health status" "$(printf '%s' "$BODY" | grep -oE '"status":"[a-z]+"' | head -1)"

  # The discriminator that Stage 1's pre-check relies on: with the internal
  # origin set, a protected read reaches authentication and answers 401. Without
  # it, getSupabaseServerUrl() throws and the answer is 503 — an image that
  # serves /login and nothing real.
  S="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://${SMOKE_PORT}/api/accounts" || echo 000)"
  [[ "$S" == "401" ]] && ok "smoke: protected read reaches auth" "http 401" \
                      || bad "smoke: protected read" "http $S (expected 401 — is SUPABASE_SERVER_URL set?)"
  S="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
      -H 'Content-Type: application/json' -d '{}' "http://${SMOKE_PORT}/api/accounts" || echo 000)"
  [[ "$S" == "503" ]] && ok "smoke: POST /api/accounts refused" "http 503" \
                      || bad "smoke: POST /api/accounts" "http $S"
  # the A-1 path: the one the proxy used to skip entirely
  S="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -X DELETE \
      "http://${SMOKE_PORT}/api/accounts/abc.png" || echo 000)"
  [[ "$S" == "503" ]] && ok "smoke: DELETE /api/accounts/abc.png refused" "http 503" \
                      || bad "smoke: the extension bypass is back" "http $S"
else
  bad "smoke: the container never became ready" "last status $code"
  docker logs "nt-smoke-$$" 2>&1 | tail -10
fi
docker rm -f "nt-smoke-$$" >/dev/null 2>&1 || true

# ── 8. provenance ───────────────────────────────────────────────────────────
python3 - "$OUT" "$SOURCE_SHA" "$TREE" "$IMAGE_ID" "$TAG" > "$OUT/provenance.json" <<'PY'
import json, subprocess, sys, os, datetime
out, sha, tree, image_id, tag = sys.argv[1:6]
def run(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception: return "unavailable"
print(json.dumps({
  "artifact": "frozen containment bridge",
  "what_this_is": "A locally built OCI image, described and scanned on this host. Every digest below was computed from local bytes.",
  "what_this_is_not": [
    "not a registry digest — nothing was pushed and no registry has attested to it",
    "not a signed registry artifact",
    "the docker image ID is a local content address for the config blob, and is not interchangeable with a registry manifest digest",
  ],
  "source": {"commit": sha, "tree": tree, "extracted_with": "git archive (committed content only)"},
  "image": {"tag": tag, "local_image_id": image_id},
  "built_on": {
    "docker": run("docker", "--version"),
    "kernel": run("uname", "-sr"),
    "host": run("hostname"),
  },
  "tools": {
    "syft_image_id": run("docker", "image", "inspect", os.environ.get("NT_SYFT_IMAGE", "anchore/syft:latest"), "--format", "{{.Id}}"),
    "trivy_image_id": run("docker", "image", "inspect", os.environ.get("NT_TRIVY_IMAGE", "aquasec/trivy:latest"), "--format", "{{.Id}}"),
  },
  "sealed_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}, indent=2))
PY
ok "provenance recorded"

# ── 9. the seal ─────────────────────────────────────────────────────────────
{
  echo "FROZEN CONTAINMENT BRIDGE — SEALED ARTIFACT"
  echo
  echo "source commit   $SOURCE_SHA"
  echo "source tree     $TREE"
  echo "local image ID  $IMAGE_ID"
  echo
  echo "This is NOT a registry digest and NOT a signed registry artifact."
  echo "Nothing was pushed. Every digest here was computed from local bytes."
  echo
  echo "vulnerabilities (trivy, over versioned components only):"
  sed 's/^/  /' "$OUT/vuln-summary.txt"
  if [[ "${SCOPE_TOT_NV:-0}" -gt 0 ]]; then
    echo "  scope: ${SCOPE_TOT_NV} of ${SCOPE_TOT} SBOM components (${SCOPE_NPM_NV} of ${SCOPE_NPM} npm)"
    echo "         carry no version and cannot be matched to a CVE — most are Next's"
    echo "         vendored deps under next/dist/compiled/. The counts above are over"
    echo "         the scannable remainder only; a 0 is not proof of absence in the"
    echo "         unversioned set."
  fi
  echo
  for f in image.tar manifest.json sbom.spdx.json sbom.cyclonedx.json vulns.json provenance.json layer-digests.txt; do
    [[ -f "$OUT/$f" ]] && echo "DIGEST $(sha256sum "$OUT/$f" | cut -d' ' -f1)  $f"
  done
} > "$OUT/SEALED.txt"
ok "SEALED.txt written"

echo
echo "seal: $OK ok, $BAD failed"
echo "artifact: $OUT"
[[ $BAD -eq 0 ]] && { echo "SEAL GREEN"; exit 0; } || { echo "SEAL RED"; exit 1; }
