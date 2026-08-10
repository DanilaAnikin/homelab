#!/usr/bin/env bash
# Stdin-only compatibility wrapper. The unprivileged caller never sees Telegram
# credentials; a socket-activated, sandboxed systemd service owns the transport.
set -euo pipefail

if (( $# != 0 )); then
  echo "notify: zpráva je povolena pouze přes stdin" >&2
  exit 64
fi

exec /usr/local/libexec/homelab-telegram-notify-client
