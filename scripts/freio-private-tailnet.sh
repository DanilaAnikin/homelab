#!/usr/bin/env bash
# Idempotent Tailscale-only listener for Freio operator hosts.
set -euo pipefail

readonly COMPOSE_FILE=/srv/homelab/compose/freio-private-proxy/docker-compose.yml
readonly MAIN_GUARD_SOURCE=/srv/homelab/compose/traefik/freio-private-hosts.yml
readonly MAIN_GUARD_RUNTIME=/etc/dokploy/traefik/dynamic/freio-private-hosts.yml
readonly POSTIZ_ORIGIN_SOURCE=/srv/homelab/traefik-dynamic-postiz.yml
readonly POSTIZ_ORIGIN_RUNTIME=/etc/dokploy/traefik/dynamic/postiz.yml
readonly POSTIZ_ACCESS_LOGIN_PREFIX='https://royal-credit-ede5.cloudflareaccess.com/cdn-cgi/access/login/postiz.freio.cz?'
readonly PROJECT=freio-private-proxy
readonly TARGET=tcp://127.0.0.1:9443
readonly LOCK_FILE=/run/lock/freio-private-tailnet.lock
readonly READINESS_ATTEMPTS=60
readonly READINESS_DELAY_SECONDS=2

probe_private_status() {
  local hostname="$1"
  local path="$2"

  curl --silent --show-error \
    --connect-to "${hostname}:443:127.0.0.1:9443" \
    --connect-timeout 5 --max-time 15 \
    --output /dev/null --write-out '%{http_code}' \
    "https://${hostname}${path}"
}

probe_main_status() {
  local scheme="$1"
  local hostname="$2"
  local path="${3:-/}"
  local port

  case "$scheme" in
    http) port=80 ;;
    https) port=443 ;;
    *) return 64 ;;
  esac

  curl --silent --show-error \
    --connect-to "${hostname}:${port}:127.0.0.1:${port}" \
    --connect-timeout 5 --max-time 15 \
    --output /dev/null --write-out '%{http_code}' \
    "${scheme}://${hostname}${path}"
}

check_postiz_access_edge() {
  local answer
  local public_ip
  local probe
  local status
  local redirect_url

  public_ip=''
  while IFS= read -r answer; do
    if [[ -z "$public_ip" && "$answer" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      public_ip="$answer"
    fi
  done < <(dig +short @1.1.1.1 postiz.freio.cz A)
  [[ -n "$public_ip" ]] || return 1

  probe=$(curl --silent --show-error \
    --resolve "postiz.freio.cz:443:${public_ip}" \
    --connect-timeout 5 --max-time 15 \
    --output /dev/null --write-out $'%{http_code}\n%{redirect_url}' \
    https://postiz.freio.cz/)
  status=${probe%%$'\n'*}
  redirect_url=${probe#*$'\n'}
  [[ "$status" == 302 && "$redirect_url" == "$POSTIZ_ACCESS_LOGIN_PREFIX"* ]] \
    || return 1

  probe=$(curl --silent --show-error \
    --resolve "postiz.freio.cz:443:${public_ip}" \
    --connect-timeout 5 --max-time 15 \
    --output /dev/null --write-out $'%{http_code}\n%{redirect_url}' \
    https://postiz.freio.cz/uploads/1970/01/01/0000000000000000000000000000000000000000000000000000000000000000.png)
  status=${probe%%$'\n'*}
  redirect_url=${probe#*$'\n'}
  [[ "$status" == 404 && -z "$redirect_url" ]]
}

wait_for_dependencies() {
  local attempt
  for ((attempt = 1; attempt <= READINESS_ATTEMPTS; attempt++)); do
    if docker info >/dev/null 2>&1 \
      && docker network inspect dokploy-network >/dev/null 2>&1 \
      && tailscale status --json 2>/dev/null \
        | jq -e '.BackendState == "Running"' >/dev/null; then
      return 0
    fi
    sleep "$READINESS_DELAY_SECONDS"
  done

  printf 'Docker/dokploy-network/Tailscale did not become ready within %s seconds.\n' \
    "$((READINESS_ATTEMPTS * READINESS_DELAY_SECONDS))" >&2
  return 1
}

check_runtime() {
  local outreach_status
  local posty_status
  local postiz_status
  local guard_status

  cmp --silent "$MAIN_GUARD_SOURCE" "$MAIN_GUARD_RUNTIME"
  cmp --silent "$POSTIZ_ORIGIN_SOURCE" "$POSTIZ_ORIGIN_RUNTIME"
  docker info >/dev/null
  docker network inspect dokploy-network >/dev/null
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" ps --status running --services \
    | grep -Fxq proxy
  docker inspect freio-private-proxy \
    | jq -e '.[0].NetworkSettings.Ports["443/tcp"] == [{"HostIp":"127.0.0.1","HostPort":"9443"}]' \
      >/dev/null
  tailscale status --json | jq -e '.BackendState == "Running"' >/dev/null
  tailscale serve status --json \
    | jq -e '
        (.TCP == {"443":{"TCPForward":"127.0.0.1:9443"}})
        and ((.Web // {}) == {})
        and ((.AllowFunnel // {}) == {})
        and ((.Foreground // null) == null)
      ' >/dev/null

  # Exercise the private proxy itself. A normal hostname request from this host
  # hairpins to the main Traefik listener instead of Tailscale Serve.
  outreach_status=$(probe_private_status outreach.freio.cz /)
  [[ "$outreach_status" == 200 ]] || {
    printf 'Unexpected private outreach.freio.cz status: %s (expected 200).\n' \
      "$outreach_status" >&2
    return 1
  }
  posty_status=$(probe_private_status posty.freio.cz /)
  [[ "$posty_status" == 401 ]] || {
    printf 'Unexpected private posty.freio.cz status: %s (expected 401).\n' \
      "$posty_status" >&2
    return 1
  }
  posty_status=$(probe_private_status posty.freio.cz /health)
  [[ "$posty_status" == 200 ]] || {
    printf 'Unexpected private posty.freio.cz health status: %s (expected 200).\n' \
      "$posty_status" >&2
    return 1
  }
  postiz_status=$(probe_private_status postiz.freio.cz /)
  case "$postiz_status" in
    200|301|302|303|307|308) ;;
    *)
      printf 'Unexpected private postiz.freio.cz status: %s.\n' "$postiz_status" >&2
      return 1
      ;;
  esac
  postiz_status=$(probe_private_status postiz-admin.freio.cz /)
  case "$postiz_status" in
    200|301|302|303|307|308) ;;
    *)
      printf 'Unexpected postiz-admin.freio.cz status: %s.\n' "$postiz_status" >&2
      return 1
      ;;
  esac

  # The public/main Traefik listener must fail closed for every operator host.
  # Source/runtime checksum parity above ensures this probes the reviewed guard.
  for guard_host in outreach.freio.cz posty.freio.cz postiz-admin.freio.cz; do
    for guard_scheme in http https; do
      guard_status=$(probe_main_status "$guard_scheme" "$guard_host")
      [[ "$guard_status" == 403 ]] || {
        printf 'Unexpected main-ingress %s status for %s: %s (expected 403).\n' \
          "$guard_scheme" "$guard_host" "$guard_status" >&2
        return 1
      }
    done
  done
  check_postiz_access_edge || {
    printf 'Public Postiz root is not gated by the expected Cloudflare Access login.\n' >&2
    return 1
  }
}

ensure_runtime() {
  if check_runtime >/dev/null 2>&1; then
    printf 'Freio private Tailnet ingress is already healthy.\n'
    return 0
  fi

  printf 'Freio private Tailnet ingress is unhealthy; reconciling proxy and Serve map.\n' >&2
  wait_for_dependencies
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d --wait --wait-timeout 60
  tailscale serve --tcp=443 --bg --yes "$TARGET"
  check_runtime
  printf 'Freio private Tailnet ingress recovered.\n'
}

action="${1:-up}"
exec 9>"$LOCK_FILE"
flock -x 9

case "$action" in
  up)
    [[ -f "$COMPOSE_FILE" ]] || { printf 'Missing %s\n' "$COMPOSE_FILE" >&2; exit 78; }
    wait_for_dependencies
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d --wait --wait-timeout 60
    tailscale serve --tcp=443 --bg --yes "$TARGET"
    check_runtime
    printf 'Freio private proxy is available on Tailnet TCP/443.\n'
    ;;
  down)
    compose_status=0
    serve_status=0
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" stop || compose_status=$?
    tailscale serve --tcp=443 off || serve_status=$?
    if (( compose_status != 0 || serve_status != 0 )); then
      printf 'Private ingress shutdown incomplete (compose=%d, serve=%d).\n' \
        "$compose_status" "$serve_status" >&2
      exit 1
    fi
    ;;
  check)
    check_runtime
    printf 'Freio private Tailnet ingress passed end-to-end checks.\n'
    ;;
  ensure)
    ensure_runtime
    ;;
  status)
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" ps
    tailscale serve status --json
    ;;
  *)
    printf 'Usage: %s {up|down|check|ensure|status}\n' "$0" >&2
    exit 64
    ;;
esac
