#!/usr/bin/env bash
set -euo pipefail

readonly PRIMARY_SERVICE=freio-xkgrrq
readonly FALLBACK_CONTAINERS=(freio-public-gateway-a freio-public-gateway-b)
readonly SOURCE_CONFIG=/srv/homelab/compose/traefik/freio-public-failover.yml
readonly RUNTIME_CONFIG=/etc/dokploy/traefik/dynamic/freio-public-failover.yml
readonly STATE_DIR=${STATE_DIRECTORY:-/var/lib/freio-public-failover}
readonly STATE_FILE=${STATE_DIR}/route.state
readonly EDGE_ENABLED_MARKER=/etc/freio-public-failover/edge-enabled

fail() {
  printf '{"ok":false,"error":"%s"}\n' "$1" >&2
  exit 1
}

[[ -f "$SOURCE_CONFIG" && ! -L "$SOURCE_CONFIG" ]] || fail source_config_missing
[[ -f "$RUNTIME_CONFIG" && ! -L "$RUNTIME_CONFIG" ]] || fail runtime_config_missing

source_sha=$(/usr/bin/sha256sum -- "$SOURCE_CONFIG" | /usr/bin/awk '{print $1}')
runtime_sha=$(/usr/bin/sha256sum -- "$RUNTIME_CONFIG" | /usr/bin/awk '{print $1}')
[[ "$source_sha" == "$runtime_sha" ]] || fail runtime_config_drift

for fallback_container in "${FALLBACK_CONTAINERS[@]}"; do
  fallback_health=$(
    /usr/bin/docker inspect "$fallback_container" \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
      2>/dev/null || true
  )
  [[ "$fallback_health" == healthy ]] || fail fallback_unhealthy
done

[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fail state_directory_missing

previous_route=unknown
if [[ -e "$STATE_FILE" ]]; then
  [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || fail state_file_invalid
  IFS= read -r previous_route < "$STATE_FILE" || true
  [[ "$previous_route" == primary || "$previous_route" == fallback \
    || "$previous_route" == edge-fallback ]] \
    || fail state_value_invalid
fi

persist_route() {
  local route_value=$1
  local temporary
  temporary=$(/usr/bin/mktemp "${STATE_DIR}/.route.state.XXXXXX")
  /usr/bin/chmod 0600 "$temporary"
  /usr/bin/printf '%s\n' "$route_value" > "$temporary"
  /usr/bin/mv -fT "$temporary" "$STATE_FILE"
}

desired=$(
  /usr/bin/docker service inspect "$PRIMARY_SERVICE" \
    --format '{{.Spec.Mode.Replicated.Replicas}}' 2>/dev/null || true
)
running=$(
  /usr/bin/docker service ps "$PRIMARY_SERVICE" --filter desired-state=running \
    --format '{{.CurrentState}}' 2>/dev/null | /usr/bin/grep -c '^Running' || true
)

route=primary
for public_host in freio.cz www.freio.cz; do
  redirect_headers=$(/usr/bin/mktemp)
  trap 'rm -f -- "$redirect_headers"' EXIT
  redirect_status=$(
    /usr/bin/curl --silent --show-error --max-time 15 \
      --dump-header "$redirect_headers" --output /dev/null \
      --write-out '%{http_code}' "http://${public_host}/"
  ) || fail public_http_request_failed
  [[ "$redirect_status" == 308 ]] || fail public_http_redirect_status
  /usr/bin/grep -Fqi "location: https://${public_host}/" "$redirect_headers" \
    || fail public_http_redirect_location
  /usr/bin/rm -f -- "$redirect_headers"
  trap - EXIT
done

if [[ -e "$EDGE_ENABLED_MARKER" ]]; then
  [[ -f "$EDGE_ENABLED_MARKER" && ! -L "$EDGE_ENABLED_MARKER" ]] \
    || fail edge_marker_invalid
  for public_host in freio.cz www.freio.cz; do
    edge_headers=$(/usr/bin/mktemp)
    edge_body=$(/usr/bin/mktemp)
    trap 'rm -f -- "$edge_headers" "$edge_body"' EXIT
    edge_status=$(
      /usr/bin/curl --silent --show-error --max-time 15 \
        --header 'Accept: application/json' \
        --dump-header "$edge_headers" --output "$edge_body" \
        --write-out '%{http_code}' \
        "https://${public_host}/__freio-edge-health"
    ) || fail edge_health_request_failed
    [[ "$edge_status" == 200 ]] || fail edge_health_status
    /usr/bin/grep -Fqi 'x-freio-edge-fallback: health-v1' "$edge_headers" \
      || fail edge_health_header_mismatch
    /usr/bin/grep -Fq '"component":"freio-edge-fallback"' "$edge_body" \
      || fail edge_health_body_mismatch
    /usr/bin/grep -Fq '"mode":"standby"' "$edge_body" \
      || fail edge_health_body_mismatch
    /usr/bin/rm -f -- "$edge_headers" "$edge_body"
    trap - EXIT
  done
fi

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
  if /usr/bin/grep -Fqi 'x-freio-edge-fallback: static-v1' "$headers"; then
    route=edge-fallback
    /usr/bin/grep -Fq 'Záložní režim je aktivní' "$body" \
      || fail edge_fallback_body_mismatch
  elif /usr/bin/grep -Fqi 'x-freio-fallback: static-v1' "$headers"; then
    route=fallback
    /usr/bin/grep -Fq 'Záložní režim je aktivní' "$body" \
      || fail fallback_body_mismatch
  fi
  /usr/bin/rm -f -- "$headers" "$body"
  trap - EXIT
done

if [[ "$desired" != 1 || "$running" -lt 1 || "$route" != primary ]]; then
  incident_state=fallback
  [[ "$route" == edge-fallback ]] && incident_state=edge-fallback
  persist_route "$incident_state"
  printf '{"ok":false,"public":true,"route":"%s","primary_desired":"%s","primary_running":%s,"fallback":"healthy"}\n' \
    "$route" "${desired:-unknown}" "${running:-0}" >&2
  if [[ "$previous_route" != "$incident_state" ]]; then
    exit 2
  fi
  exit 0
fi

persist_route primary
printf '{"ok":true,"public":true,"route":"primary","recovered":%s,"primary_desired":"1","primary_running":%s,"fallback":"healthy","config_sha256":"%s"}\n' \
  "$([[ "$previous_route" == fallback ]] && printf true || printf false)" \
  "$running" "$runtime_sha"
