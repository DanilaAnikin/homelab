#!/usr/bin/env bash
# ============================================================================
# Fetch the official Supabase self-hosting Docker files into ./supabase-docker/.
#
# We deliberately DO NOT hand-copy the upstream compose: it relies on the
# volumes/ init scripts (which create the Supabase roles: anon, authenticated,
# service_role, supabase_admin, …) and the Kong config — reproducing those by
# hand drifts and breaks Auth/Storage. So we vendor the real thing at a PINNED
# commit and layer our .env + docker-compose.override.yml on top.
#
# After this runs, see README.md for the (short) hardening edits + launch.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# ⚠️ PIN THIS. Set to a specific commit SHA of supabase/supabase after you've
# reviewed docker/versions.md at that commit. "master" is a moving target — only
# use it to bootstrap, then pin. Image tags verified against master 2026-07:
#   db 17.6.1.136 · kong 3.9.1 · auth v2.189.0 · rest v14.12 · realtime v2.102.3
#   storage v1.60.4 · imgproxy v3.30.1 · meta v0.96.6 · studio 2026.07.07-...
REF="${SUPABASE_REF:-master}"
DEST="supabase-docker"

echo "==> Sparse-fetching supabase/supabase 'docker/' at ref: $REF"
rm -rf .tmp-supabase "$DEST"
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/supabase/supabase.git .tmp-supabase
git -C .tmp-supabase sparse-checkout set docker
if [[ "$REF" != "master" ]]; then
  git -C .tmp-supabase fetch --depth 1 origin "$REF"
  git -C .tmp-supabase checkout "$REF"
fi
mv .tmp-supabase/docker "$DEST"
rm -rf .tmp-supabase

echo "==> Fetched into $DEST/"
echo "    Pinned commit: $(cd "$DEST/.." && echo "$REF")"
cat <<'EOF'

Next (see README.md for detail):
  1) Review supabase-docker/docker-compose.yml and versions.md.
  2) Hardening edits on the fetched base:
       - remove the `supavisor` service (we don't use the pooler; it also
         publishes host ports 5432/6543).
       - confirm there is NO `analytics`/`vector` service (master has none).
       - confirm `db` publishes no host port (it shouldn't).
  3) cp .env.example .env  → fill in (key-gen commands are in .env.example), chmod 600 .env
  4) docker compose -f supabase-docker/docker-compose.yml -f docker-compose.override.yml up -d
  5) Point Cloudflare Tunnel: supabase.<domain> -> kong:8000, and BLOCK /
     and /pg/* at the edge (Cloudflare Access) — see README "Studio".
EOF
