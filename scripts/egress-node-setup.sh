#!/usr/bin/env bash
# ============================================================================
# Egress mail node bootstrap — a small VPS (or a friend's box) that becomes
# launchmail's direct-delivery sender. Installs Docker + Tailscale and checks
# the two things only the provider can give us: a PTR record and open port 25.
# Run as root on a fresh Ubuntu/Debian host:  sudo bash egress-node-setup.sh
# ============================================================================
set -euo pipefail
HELO="${MAIL_HELO_HOSTNAME:-mail.ripieno.xyz}"
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/4] Base packages"
apt-get update -y
apt-get install -y curl ca-certificates netcat-openbsd dnsutils

echo "==> [2/4] Docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

echo "==> [3/4] Tailscale (reaches the homelab Postgres/Redis over the tailnet)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
echo "    → run:  tailscale up   (then confirm you can reach the homelab)"

echo "==> [4/4] Deliverability preflight"
PUBIP=$(curl -fsS --max-time 10 https://ifconfig.me || echo "?")
echo "    Public IP: $PUBIP"

echo -n "    PTR (reverse DNS): "
PTR=$(dig +short -x "$PUBIP" 2>/dev/null || true)
if [[ -n "$PTR" ]]; then
  echo "$PTR"
  [[ "${PTR%.}" == "$HELO" ]] && echo "    ✔ PTR matches $HELO" \
    || echo "    ✘ PTR is not $HELO — set it in your provider's console!"
else
  echo "NONE — set PTR = $HELO in your provider's console (rDNS)"
fi

echo -n "    Outbound port 25: "
if timeout 8 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  echo "✔ OPEN"
else
  echo "✘ BLOCKED — ask your provider to unlock outbound port 25 (often a ticket)"
fi

cat <<EOF

════════════════════════════════════════════════════════════
 Next:
   1) tailscale up        # join the tailnet; verify homelab is reachable
   2) Fix any ✘ above (PTR + port 25 — provider-side, may take a day)
   3) cd compose/mail-egress && cp .env.example .env && edit + chmod 600 .env
   4) docker compose up -d --build
   5) In launchmail UI: create a Direct SMTP config, HELO = $HELO,
      make it the default. Publish the DNS records from the
      Deliverability panel. Then send a test to mail-tester.com.
   6) On the HOMELAB, switch launchmail's api to run without a worker
      (start instead of start:all) so only THIS node sends.
════════════════════════════════════════════════════════════
EOF
