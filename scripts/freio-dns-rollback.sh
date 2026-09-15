#!/usr/bin/env bash
# Vrátí freio.cz a www.freio.cz z dočasné maintenance stránky na Vercelu
# zpátky na Cloudflare Tunnel homelabu.
#
# Kontext: 2026-09-09 v 08:25 UTC odpadl homelab ze sítě, tunel přestal
# odpovídat (Cloudflare 1033) a oba záznamy byly přesměrovány na statickou
# stránku o výpadku. Tenhle skript ten přesun vrací. Spusť ho, AŽ tunel zase
# jede — jinak si vrátíš chybu 1033 místo stránky o údržbě.
#
# Nesahá na MX, TXT ani na ostatní subdomény.
set -euo pipefail

TOK="$(tr -d '\n' < ~/programming/homelab/secrets/cloudflare-api-token.txt)"
ZID=d95c4ea3d5dd9dae397ec4ff3ccd23c2
TUNNEL=215c5edb-467c-470a-9e34-1d46e65fcfef.cfargotunnel.com

put(){ # id name
  curl -fsS -X PUT "https://api.cloudflare.com/client/v4/zones/$ZID/dns_records/$1" \
    -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    -d "{\"type\":\"CNAME\",\"name\":\"$2\",\"content\":\"$TUNNEL\",\"ttl\":1,\"proxied\":true}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=d.get('result') or {}
print(('  OK   ' if d.get('success') else '  CHYBA ')+f\"{r.get('name','?')} -> {r.get('content','?')} proxied={r.get('proxied')}\")
sys.exit(0 if d.get('success') else 1)
"
}

echo "Vracím freio.cz a www.freio.cz na tunel..."
put 294fb48ba5432a3355f787b46232c112 freio.cz
put 296d02c3c90d9d0e8a6bb50e9444a729 www.freio.cz

echo
echo "Ověření (může chvíli trvat, než odejde DNS cache):"
for h in freio.cz www.freio.cz; do
  printf '  %-14s HTTP %s\n' "$h" "$(curl -s -o /dev/null -w '%{http_code}' "https://$h" || echo '---')"
done
echo
echo "Až to bude vracet 200, můžeš smazat Vercel projekt:"
echo "  vercel remove freio-maintenance --yes"
