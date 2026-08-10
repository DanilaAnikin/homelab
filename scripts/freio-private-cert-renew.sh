#!/usr/bin/env bash
# Renew the public-trust certificate used by Tailscale-only Freio operator hosts.
set -euo pipefail

readonly CERT_ROOT=/etc/dokploy/traefik/dynamic/freio-private-certs
readonly DYNAMIC_CONFIG=/etc/dokploy/traefik/dynamic/freio-private-hosts.yml
readonly PRIVATE_PROXY_CONFIG=/srv/homelab/compose/freio-private-proxy/dynamic.yml
readonly TOKEN_FILE=/etc/freio-private-ingress/cloudflare-dns-token.txt
readonly LOCK_FILE=/run/lock/freio-private-cert-renew.lock
readonly LEGO_IMAGE='goacme/lego@sha256:f4fd80df0ef94d2f536cc2e7fb5bdbd090fb0aa81b3595226b9fe814bb9a2bfe'
readonly CERT_FILE="$CERT_ROOT/certificates/outreach.freio.cz.crt"
readonly LEGO_CONTAINER="freio-private-cert-renew-$$"

cleanup() {
  docker rm -f "$LEGO_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

exec 9>"$LOCK_FILE"
flock -n 9 || { printf 'Freio private certificate renewal is already running.\n'; exit 0; }

[[ -s "$TOKEN_FILE" ]] || { printf 'Missing Cloudflare DNS token: %s\n' "$TOKEN_FILE" >&2; exit 78; }
[[ -f "$DYNAMIC_CONFIG" ]] || { printf 'Missing Traefik config: %s\n' "$DYNAMIC_CONFIG" >&2; exit 78; }
docker info >/dev/null

install -d -o root -g root -m 0700 "$CERT_ROOT"
umask 077

docker run --rm --name "$LEGO_CONTAINER" \
  -e CF_DNS_API_TOKEN_FILE=/run/secrets/cf-token \
  -v "$TOKEN_FILE:/run/secrets/cf-token:ro" \
  -v "$CERT_ROOT:/data" \
  "$LEGO_IMAGE" run \
  --accept-tos \
  --email contact@freio.cz \
  --dns cloudflare \
  --dns.propagation.wait 10s \
  --path /data \
  --renew-days 30 \
  --no-random-sleep \
  --domains outreach.freio.cz \
  --domains posty.freio.cz \
  --domains postiz.freio.cz \
  --domains postiz-admin.freio.cz

find "$CERT_ROOT" -type d -exec chmod 0700 {} +
find "$CERT_ROOT" -type f -exec chmod 0600 {} +
openssl x509 -in "$CERT_FILE" -noout -checkend 604800
for expected_san in outreach.freio.cz posty.freio.cz postiz.freio.cz postiz-admin.freio.cz; do
  openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName \
    | grep -Fq "DNS:$expected_san"
done

# The file provider notices the mtime change and reloads the renewed keypair.
touch "$DYNAMIC_CONFIG"
[[ ! -f "$PRIVATE_PROXY_CONFIG" ]] || touch "$PRIVATE_PROXY_CONFIG"
printf 'Freio private certificate is valid for at least seven more days.\n'
