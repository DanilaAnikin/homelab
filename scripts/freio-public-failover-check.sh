#!/usr/bin/env bash
set -euo pipefail

readonly PRIMARY_SERVICE=freio-xkgrrq
readonly FALLBACK_CONTAINER=freio-public-fallback
readonly SOURCE_CONFIG=/srv/homelab/compose/traefik/freio-public-failover.yml
readonly RUNTIME_CONFIG=/etc/dokploy/traefik/dynamic/freio-public-failover.yml

fail() {
  printf '{"ok":false,"error":"%s"}\n' "$1" >&2
  exit 1
}

[[ -f "$SOURCE_CONFIG" && ! -L "$SOURCE_CONFIG" ]] || fail source_config_missing
[[ -f "$RUNTIME_CONFIG" && ! -L "$RUNTIME_CONFIG" ]] || fail runtime_config_missing

source_sha=$(/usr/bin/sha256sum -- "$SOURCE_CONFIG" | /usr/bin/awk '{print $1}')
runtime_sha=$(/usr/bin/sha256sum -- "$RUNTIME_CONFIG" | /usr/bin/awk '{print $1}')
[[ "$source_sha" == "$runtime_sha" ]] || fail runtime_config_drift

fallback_health=$(
  /usr/bin/docker inspect "$FALLBACK_CONTAINER" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    2>/dev/null || true
)
[[ "$fallback_health" == healthy ]] || fail fallback_unhealthy

desired=$(
  /usr/bin/docker service inspect "$PRIMARY_SERVICE" \
    --format '{{.Spec.Mode.Replicated.Replicas}}' 2>/dev/null || true
)
running=$(
  /usr/bin/docker service ps "$PRIMARY_SERVICE" --filter desired-state=running \
    --format '{{.CurrentState}}' 2>/dev/null | /usr/bin/grep -c '^Running' || true
)

route=primary
for origin in https://freio.cz/ https://www.freio.cz/; do
  headers=$(/usr/bin/mktemp)
  body=$(/usr/bin/mktemp)
  trap 'rm -f -- "$headers" "$body"' EXIT
  status=$(
    /usr/bin/curl --silent --show-error --location --max-time 15 \
      --dump-header "$headers" --output "$body" --write-out '%{http_code}' \
      "$origin"
  ) || fail public_request_failed
  [[ "$status" == 200 ]] || fail public_status_not_200
  if /usr/bin/grep -qi '^x-freio-fallback: static-v1' "$headers"; then
    route=fallback
    /usr/bin/grep -Fq 'Záložní režim je aktivní' "$body" \
      || fail fallback_body_mismatch
  fi
  /usr/bin/rm -f -- "$headers" "$body"
  trap - EXIT
done

if [[ "$desired" != 1 || "$running" -lt 1 || "$route" != primary ]]; then
  printf '{"ok":false,"public":true,"route":"%s","primary_desired":"%s","primary_running":%s,"fallback":"healthy"}\n' \
    "$route" "${desired:-unknown}" "${running:-0}" >&2
  exit 2
fi

printf '{"ok":true,"public":true,"route":"primary","primary_desired":"1","primary_running":%s,"fallback":"healthy","config_sha256":"%s"}\n' \
  "$running" "$runtime_sha"
