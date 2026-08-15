#!/usr/bin/env bash
# ============================================================================
# Does the Stage 2 auth-only edge actually deny the data plane?
#
# The overlay is a path matcher, and a path matcher is exactly the kind of
# control that looks obviously correct and is not. The interesting question is
# never "does /rest/v1 get a 403" — it is whether anything can satisfy the
# allow rule and still be resolved by the backend as a different path:
#
#     /auth/v1/../rest/v1/accounts
#     /auth/v1/..%2Frest%2Fv1%2Faccounts
#     /auth/v1/%252e%252e/rest/v1/accounts
#
# Whether those are safe depends on whether this specific Traefik build
# normalises before matching and forwards the normalised form. That is a fact
# about a binary, not something to reason about from documentation — and when
# this was first run it found two live bypasses that the first draft of the
# overlay let through. So it runs the REAL production Traefik version against
# a backend that echoes the path it actually received.
#
# THE ORACLE IS THE BACKEND, NOT THE STATUS CODE
# ----------------------------------------------
# A 403 is weak evidence: a probe could be denied for an unrelated reason while
# a different probe sails through. The property that matters is that the
# backend NEVER SEES a path outside /auth/v1. whoami echoes the raw request
# line it received, so every denied probe is checked against what actually
# arrived, not against what Traefik chose to answer. That distinction is what
# caught the encoded-slash bypass: it returned 200, and the echoed line showed
# `/auth/v1/..%2Frest%2Fv1%2Faccounts` sitting at the backend for Kong to
# decode.
#
# Nothing here touches production. A private docker network, a throwaway
# Traefik, a throwaway backend, all torn down on exit.
# ============================================================================
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY="$HERE/../nt-b4-stage2-overlay.yml"
TRAEFIK_VERSION="${NT_TRAEFIK_VERSION:-v3.6.7}"   # must match production
WORK="$(mktemp -d "${TMPDIR:-/tmp}/b4-stage2.XXXXXX")"
NET="nt-b4-stage2-$$"
TFK="nt-b4-tfk-$$"
BACKEND="natetrader-supabase-kong"   # the overlay names this host; the stand-in must answer to it

cleanup(){
  docker rm -f "$TFK" "$BACKEND-$$" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

OK=0; BAD=0
note(){ printf '  %-6s %-52s %s\n' "$1" "$2" "${3:-}"; }
pass(){ OK=$((OK+1)); note ok "$1" "${2:-}"; }
fail(){ BAD=$((BAD+1)); note NOT-OK "$1" "${2:-}"; }

[[ -f "$OVERLAY" ]] || { echo "missing $OVERLAY"; exit 1; }

echo "Stage 2 auth-only edge — matcher behaviour under Traefik $TRAEFIK_VERSION"
echo

# ── stand up a private edge ─────────────────────────────────────────────────
mkdir -p "$WORK/dynamic"
# The real overlay, with only the backend URL repointed at the stand-in. The
# RULES — the thing under test — are copied verbatim.
sed "s|http://natetrader-supabase-kong:8000|http://$BACKEND-$$:80|; \
     s|http://natetrader-dashboard:3000|http://$BACKEND-$$:80|" \
    "$OVERLAY" > "$WORK/dynamic/natetrader.yml"

# the rules must have survived the substitution unchanged
if diff <(grep -E 'rule:|priority:|sourceRange:|- "255' "$OVERLAY") \
        <(grep -E 'rule:|priority:|sourceRange:|- "255' "$WORK/dynamic/natetrader.yml") >/dev/null; then
  pass "rules copied verbatim from the real overlay"
else
  fail "the substitution altered a rule — the test would not be testing the overlay"
  exit 1
fi

cat > "$WORK/traefik.yml" <<EOF
entryPoints:
  web:
    address: ":80"
providers:
  file:
    directory: /dynamic
    watch: true
log:
  level: INFO
EOF

docker network create "$NET" >/dev/null
docker run -d --name "$BACKEND-$$" --network "$NET" traefik/whoami:latest >/dev/null
docker run -d --name "$TFK" --network "$NET" -p 127.0.0.1:18080:80 \
  -v "$WORK/traefik.yml:/etc/traefik/traefik.yml:ro" \
  -v "$WORK/dynamic:/dynamic:ro" \
  "traefik:$TRAEFIK_VERSION" >/dev/null

# Wait for the CONDITION, not for the process.
#
# `curl` succeeding proves only that something accepted the connection —
# Traefik answers 404 for several seconds while the file provider loads, and a
# readiness check that treats "any response" as ready starts the suite against
# an edge with no routers, where every deny passes for the wrong reason. Poll
# for the status the positive control is about to assert.
ready=0
for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 --path-as-is \
          -H 'Host: ntapi.anikin.cz' http://127.0.0.1:18080/auth/v1/settings 2>/dev/null || echo 000)"
  [[ "$code" == "200" ]] && { ready=1; break; }
  sleep 0.5
done
if [[ "$ready" == 1 ]]; then
  pass "edge became ready" "/auth/v1/settings -> 200"
else
  fail "edge never became ready" "last status $code"
  docker logs "$TFK" 2>&1 | tail -20
  exit 1
fi

# ── probe helper ────────────────────────────────────────────────────────────
# --path-as-is so curl sends what we wrote; otherwise curl resolves ../ itself
# and we would be testing curl, not Traefik.
probe(){ # <raw-path> -> "<status>|<what the backend saw, or ->"
  local raw="$1" out status body seen
  out="$(curl -sS --path-as-is --max-time 10 -o "$WORK/body" -w '%{http_code}' \
        -H 'Host: ntapi.anikin.cz' "http://127.0.0.1:18080$raw" 2>/dev/null || echo "000")"
  status="$out"
  body="$(cat "$WORK/body" 2>/dev/null || true)"
  # whoami echoes the raw request line it received — "GET /path HTTP/1.1" —
  # which is the strongest available oracle: it is the literal target Traefik
  # forwarded, after whatever normalisation Traefik chose to apply.
  seen="$(printf '%s' "$body" | tr -d '\r' | sed -n 's|^[A-Z]\{3,7\} \(/[^ ]*\) HTTP/.*|\1|p' | head -1)"
  printf '%s|%s' "$status" "${seen:--}"
}

# ── 0. non-vacuity: the edge must actually be up and forwarding ─────────────
r="$(probe /auth/v1/settings)"
if [[ "${r%%|*}" == "200" && "${r#*|}" == "/auth/v1/settings" ]]; then
  pass "positive control: the edge forwards to the backend" "$r"
else
  fail "positive control FAILED — nothing below means anything" "$r"
  docker logs "$TFK" 2>&1 | tail -15
  exit 1
fi

# ── 1. the auth surface must work, or the cutover takes login down ──────────
for p in /auth/v1 /auth/v1/ /auth/v1/settings /auth/v1/token /auth/v1/user \
         /auth/v1/logout /auth/v1/verify /auth/v1/recover \
         "/auth/v1/token?grant_type=password" \
         "/auth/v1/verify?redirect_to=https%3A%2F%2Fnate-trader.anikin.cz%2F" \
         "/auth/v1/authorize?provider=github&redirect_to=https%3A%2F%2Fx%2Fcb" ; do
  r="$(probe "$p")"
  if [[ "${r%%|*}" == "200" ]]; then pass "ALLOW $p" "reached backend"
  else fail "ALLOW $p" "got $r — this would break login"; fi
done

# ── 2. the data plane must not be reachable ─────────────────────────────────
# For each of these the backend must see NOTHING. A 403 with the backend
# untouched is the pass; anything that arrives at the backend is a bypass even
# if Traefik also returned an error.
denied(){ # <label> <raw-path>
  local r status seen
  r="$(probe "$2")"; status="${r%%|*}"; seen="${r#*|}"
  if [[ "$seen" != "-" ]]; then
    fail "DENY $1" "BYPASS — backend saw '$seen' (http $status)"
  elif [[ "$status" == "403" ]]; then
    pass "DENY $1" "403, backend saw nothing"
  else
    # not a bypass, but not the intended answer either
    fail "DENY $1" "http $status (expected 403), backend saw nothing"
  fi
}

# the Supabase data plane, plainly addressed
denied "kong root"        /
denied "rest root"        /rest/v1/
denied "rest table"       /rest/v1/accounts
denied "rpc"              /rest/v1/rpc/vault_create_secret
denied "graphql"          /graphql/v1
denied "storage"          /storage/v1/object/public/x
denied "realtime"         /realtime/v1/websocket
denied "functions"        /functions/v1/hello
denied "pg meta"          /pg/

# prefix confusion — the reason the rule uses a trailing slash
denied "slashless prefix" /auth/v1x
denied "lookalike host path" /auth/v1-is-not-auth/token
denied "auth as substring" /notauth/v1/token
denied "auth deeper"      /x/auth/v1/token

# traversal, raw and encoded — the attacks a path matcher actually loses to
denied "traversal"          /auth/v1/../rest/v1/accounts
denied "traversal x2"       /auth/v1/../../rest/v1/accounts
denied "encoded traversal"  /auth/v1/%2e%2e/rest/v1/accounts
denied "encoded slash"      /auth/v1/..%2frest%2fv1%2faccounts
denied "double encoded"     /auth/v1/%252e%252e/rest/v1/accounts
denied "leading double //"  //auth/v1/../rest/v1/accounts
denied "dot segment"        /./auth/v1/../rest/v1/accounts
denied "semicolon param"    "/auth/v1;/../rest/v1/accounts"
denied "backslash"          '/auth/v1\..\rest\v1\accounts'

# case — Traefik path matching is case-sensitive, so these must land in deny
denied "upper AUTH"       /AUTH/v1/token
denied "upper V1"         /auth/V1/token

# ── 3. the dashboard host is untouched by the deny ──────────────────────────
dash="$(curl -sS --path-as-is --max-time 10 -o "$WORK/dbody" -w '%{http_code}' \
        -H 'Host: nate-trader.anikin.cz' http://127.0.0.1:18080/api/health 2>/dev/null || echo 000)"
if [[ "$dash" == "200" ]]; then pass "dashboard host still routes" "http 200"
else fail "dashboard host broke" "http $dash"; fi

# ── 4. falsification: remove the middleware and the data plane must open ────
# If /rest/v1 were unreachable for some reason OTHER than this middleware,
# every DENY above would be a false positive.
sed 's/      middlewares: \[natetrader-deny-data-plane\]//' \
    "$WORK/dynamic/natetrader.yml" > "$WORK/dynamic/.tmp" && mv "$WORK/dynamic/.tmp" "$WORK/dynamic/natetrader.yml"
for _ in $(seq 1 20); do
  r="$(probe /rest/v1/accounts)"; [[ "${r#*|}" != "-" ]] && break; sleep 0.5
done
if [[ "${r#*|}" == "/rest/v1/accounts" ]]; then
  pass "falsification: without the middleware the data plane IS reachable" "the denies above are real"
else
  fail "falsification: /rest/v1 unreachable even with the middleware removed" "the DENY results prove nothing ($r)"
fi

echo
echo "stage 2 matcher: $OK ok, $BAD not-ok"
[[ $BAD -eq 0 ]] && { echo "MATCHER GREEN — only /auth/v1 crosses the edge"; exit 0; } \
                 || { echo "MATCHER RED"; exit 1; }
