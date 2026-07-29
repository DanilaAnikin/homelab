#!/usr/bin/env bash
# notify.sh "<zpráva>" — pošle Telegram zprávu majiteli (self-healing eskalace,
# backup fail, atd.). Sdílený notifikační kanál homelabu. Bezpečné volat odkudkoli.
set -uo pipefail
MSG="${1:-(prázdná zpráva)}"
TOKEN_FILE="/srv/frem/telegram-token"
CHAT_ID="1692631422"
[[ -r "$TOKEN_FILE" ]] || { echo "notify: chybí $TOKEN_FILE" >&2; exit 1; }
TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
curl -fsS -m 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=🏠 homelab: ${MSG}" \
  -o /dev/null && echo "notify: odesláno" || { echo "notify: SELHALO" >&2; exit 1; }
