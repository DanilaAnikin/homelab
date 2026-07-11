#!/usr/bin/env bash
# ============================================================================
# HOMELAB BOOTSTRAP — Ubuntu Server 24.04
# Spusť jako root na čerstvé instalaci:   sudo bash bootstrap.sh
# Idempotentní — lze pustit opakovaně.
# Co udělá: update, user+SSH klíč, SSH hardening, UFW, fail2ban,
#           auto-updates, docker log-rotace, Tailscale, Dokploy, cloudflared.
# ============================================================================
set -euo pipefail

# ── Nastavení ──────────────────────────────────────────────────────────────
NEW_USER="anakin"
SSH_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEp/SbZ01A9XspWVE7bx+eSNr66xR6WZuwIBAcMyoD8r danila.s.anikin@gmail.com'
TIMEZONE="Europe/Prague"

[[ $EUID -eq 0 ]] || { echo "Spusť jako root: sudo bash $0"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/9] Aktualizace systému a základní nástroje"
apt-get update -y
apt-get full-upgrade -y
apt-get install -y curl wget git ufw fail2ban unattended-upgrades \
  htop ncdu jq rclone smartmontools ca-certificates gnupg rsync

echo "==> [2/9] Časová zóna"
timedatectl set-timezone "$TIMEZONE"

echo "==> [3/9] Uživatel $NEW_USER + SSH klíč"
id "$NEW_USER" &>/dev/null || adduser --disabled-password --gecos "" "$NEW_USER"
usermod -aG sudo "$NEW_USER"
install -d -m 700 -o "$NEW_USER" -g "$NEW_USER" "/home/$NEW_USER/.ssh"
touch "/home/$NEW_USER/.ssh/authorized_keys"
grep -qF "$SSH_PUBKEY" "/home/$NEW_USER/.ssh/authorized_keys" || \
  echo "$SSH_PUBKEY" >> "/home/$NEW_USER/.ssh/authorized_keys"
chown "$NEW_USER:$NEW_USER" "/home/$NEW_USER/.ssh/authorized_keys"
chmod 600 "/home/$NEW_USER/.ssh/authorized_keys"
# sudo bez hesla (přihlášení jen klíčem, heslo se nepoužívá)
echo "$NEW_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$NEW_USER"
chmod 440 "/etc/sudoers.d/90-$NEW_USER"

echo "==> [4/9] SSH hardening (jen klíče, žádný root)"
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
EOF
systemctl restart ssh

echo "==> [5/9] Firewall (UFW) + fail2ban"
# Pozn.: porty publikované Dockerem UFW obcházejí. Reálná ochrana z internetu
# = domácí NAT (nic se neforwarduje) + Cloudflare Tunnel (jen odchozí spojení).
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH (LAN + Tailscale)'
ufw allow in on tailscale0 comment 'Tailscale traffic'
ufw --force enable
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban

echo "==> [6/9] Automatické bezpečnostní aktualizace + strop na logy"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/size.conf
systemctl restart systemd-journald

echo "==> [7/9] Server nesmí spát + sysctl pro hodně kontejnerů"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
cat > /etc/sysctl.d/99-homelab.conf <<'EOF'
vm.swappiness = 10
fs.inotify.max_user_watches = 1048576
fs.inotify.max_user_instances = 1024
EOF
sysctl --system >/dev/null
systemctl enable --now smartd 2>/dev/null || true   # hlídání zdraví SSD

echo "==> [8/9] Docker log-rotace (musí existovat PŘED instalací Dockeru)"
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
fi

echo "==> [9/9] Tailscale + Dokploy + cloudflared"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if [[ ! -d /etc/dokploy ]]; then
  curl -sSL https://dokploy.com/install.sh | sh
else
  echo "    Dokploy už nainstalovaný — přeskakuji."
fi
if ! command -v cloudflared >/dev/null; then
  CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg > /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $CODENAME main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -y && apt-get install -y cloudflared
fi
systemctl restart docker || true   # ať si Docker načte daemon.json

install -d -o "$NEW_USER" -g "$NEW_USER" /srv/homelab
mkdir -p /mnt/backup

IP=$(hostname -I | awk '{print $1}')
cat <<EOF

════════════════════════════════════════════════════════════
 ✔ BOOTSTRAP HOTOVÝ. Ruční kroky (viz RUNBOOK.md, fáze 3–5):

   1) sudo tailscale up --ssh          # přihlášení přes URL
   2) http://$IP:3000                  # HNED založ admin účet Dokploy
   3) sudo cloudflared service install <TOKEN>   # token z CF Zero Trust
   4) infra služby:  /srv/homelab/compose/...    # docker compose up -d
════════════════════════════════════════════════════════════
EOF
