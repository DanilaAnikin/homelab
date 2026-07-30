# RUNBOOK — od krabice k běžícím projektům

Postupuj shora dolů, odškrtávej. Odhad: den D = ~1,5–2 h čistého času.

---

## Fáze 0 — TEĎ (než dorazí hardware)

- [ ] **Účty (všechny zdarma):**
  - [Cloudflare](https://dash.cloudflare.com/sign-up) — DNS, Tunnel, R2, Email Routing
  - [Tailscale](https://login.tailscale.com/start) — admin přístup (přihlaš se Googlem)
  - [Brevo](https://onboarding.brevo.com/account/register) — SMTP relay (300 mailů/den free)
  - [UptimeRobot](https://uptimerobot.com) — externí hlídání dostupnosti
- [ ] **První doména na Cloudflare:** Add site → Free plan → změň nameservery u registrátora. (Ostatní domény postupně stejně.)
- [ ] **Ubuntu ISO:** stahuje se do `iso/`; ověření: `cd ~/programming/homelab/iso && sha256sum -c SHA256SUMS --ignore-missing`
- [ ] **Flash USB klíčenky (8 GB+):**
  ```bash
  lsblk -o NAME,SIZE,MODEL,TRAN          # najdi svůj USB disk, např. sdb (TRAN=usb!)
  sudo dd if=~/programming/homelab/iso/ubuntu-24.04.4-live-server-amd64.iso \
      of=/dev/sdX bs=4M status=progress conv=fsync   # sdX NAHRAĎ! smaže celý disk!
  ```
- [ ] Volitelně kup: **externí USB SSD** na lokální zálohy (~1 TB, Samsung T7 apod.), **ethernet kabel** (server NIKDY na Wi-Fi).
- [ ] Změř upload domácí linky na [fast.com](https://fast.com) (klikni „show more info") — ať víš, co čekat.

## Fáze 1 — Ubuntu instalace (den D)

- [ ] Server zapoj **ethernetem**, připoj monitor+klávesnici, boot z USB (boot menu: `F7` nebo `Del` při startu).
- [ ] **BIOS hned nastav:** `Restore on AC Power Loss` → **Power On** (po výpadku proudu sám naběhne; bývá v Chipset/Power sekci).
- [ ] Ubuntu installer volby:
  - Language: English, keyboard: dle chuti
  - Network: ethernet přes DHCP → **poznamenej si IP**
  - Storage: **Use entire disk** a ⚠️ **ODŠKRTNI „Set up this disk as an LVM group"** (jinak Ubuntu použije jen ~100 GB disku — klasická past). Potvrdit smazání Windows ✓ (OEM/Windows bloatware tím zmizí).
  - Profile: name `anakin`, server `homelab`, user `anakin`, heslo dočasné (po bootstrapu se stejně přihlašuje jen klíčem)
  - **SSH: ✅ Install OpenSSH server**, import keys: přeskoč
  - Featured snaps: nic
- [ ] Reboot, vytáhni USB.
- [ ] **Router:** nastav DHCP rezervaci pro MAC serveru (zjistíš `ip a`), ať má napořád stejnou IP.

## Fáze 2 — Bootstrap (~15 min)

Z notebooku:
```bash
rsync -av --exclude iso ~/programming/homelab/ anakin@<IP>:~/homelab/
ssh anakin@<IP>
sudo bash ~/homelab/scripts/bootstrap.sh
```
Skript: aktualizace, SSH hardening (jen klíče), UFW+fail2ban, auto-updates, Docker log-rotace, **Tailscale + Dokploy + cloudflared**. Na konci vypíše ruční kroky.

```bash
sudo rsync -a ~/homelab/compose ~/homelab/scripts ~/homelab/secrets /srv/homelab/
```

## Fáze 3 — Tailscale + Dokploy admin (~10 min)

- [ ] `sudo tailscale up --ssh` → otevři vypsanou URL, přihlas se.
- [ ] [Tailscale admin](https://login.tailscale.com/admin/machines): u stroje `homelab` → **Disable key expiry**.
- [ ] Na notebook nainstaluj Tailscale, přihlas se → otestuj `ssh anakin@homelab`.
- [ ] **Dokploy:** otevři `http://<IP>:3000` → **HNED založ admin účet** (první registrace = admin!).
- [ ] Dokploy → Settings → zapni **Docker Cleanup** (uklízí staré images).

## Fáze 4 — Cloudflare Tunnel (~5 min)

- [x] Tunel `homelab` vytvořen 2026-07-11 (Tunnel ID `<TUNNEL_ID — viz secrets/homelab.conf>`),
      token uložen v `secrets/cloudflared-token.txt`, testovací hostname `test.ripieno.xyz → localhost:80`.
- [ ] Na serveru už jen:
  ```bash
  sudo cloudflared service install "$(cat /srv/homelab/secrets/cloudflared-token.txt)"
  systemctl status cloudflared   # active (running); v dashboardu connector zezelená
  curl -s https://test.ripieno.xyz | head -3   # ověření: odpoví Traefik (404 = tunel FUNGUJE)
  ```
- [ ] Detaily a napojení dalších domén: `docs/networking.md`.

## Fáze 5 — Infra služby (~20 min)

**Postgres + PgBouncer:**
✅ Hesla už jsou vygenerovaná předem (2026-07-11): `.env` + `pgbouncer/userlist.txt`
jsou součástí kitu (rsync je přenesl). Stačí:
```bash
cd /srv/homelab/compose/postgres
ls -l .env pgbouncer/userlist.txt      # ověř, že dorazily (jinak viz .env.example)
# ⚠️ userlist.txt MUSÍ být čitelný pro pgbouncer kontejner (běží pod jiným uid) —
#    jinak "could not open auth_file ... Permission denied". .env nech 600 (čte ho
#    jen docker compose na hostu), userlist 644 (bind-mountuje se DO kontejneru):
chmod 644 pgbouncer/userlist.txt
sudo docker compose up -d && sudo docker logs -f shared-postgres   # Ctrl+C až uvidíš "ready to accept connections"
```
> Pozn.: `pgbouncer.ini` má `auth_dbname = postgres` (auth_query běží proti DB
> `postgres`, kde je funkce `pgbouncer.get_auth`) — jinak "bouncer config error".

**E-maily — vlastní launchmail instance (bez jakékoli třetí strany):**

Vlastní kopie launchmailu žije v tomto repu (`launchmail/`) — produkce LaunchDay
se nedotýká. Doručování řeší výhradně náš vlastní pipeline (viz
`launchmail/DIRECT_DELIVERY_PLAN.md` — P1 direct-MX kód + P5 delivery node).

- [ ] V Dokploy: propoj GitHub (Settings → Git → GitHub App; repo je privátní)
- [ ] Project `infra` → **Compose service** → repo `DanilaAnikin/homelab`,
      compose path `launchmail/docker-compose.yml`, branch `main`
- [ ] Environment: nakopíruj obsah `secrets/launchmail.env` (vygenerován
      2026-07-11; ⚠️ MAIL_ENCRYPTION_KEY se po prvním startu NIKDY nesmí změnit)
- [ ] Deploy → Doména: `mail.ripieno.xyz` → service `web`, port 3000, HTTPS OFF
      (+ tunnel hostname `mail.ripieno.xyz → localhost:80` v CF, pokud není wildcard)
- [ ] Ověř: web UI naběhne, založ si admin účet, projdi Settings

**Odesílání — DEN 1: Seznam SMTP smarthost (zdarma, funguje hned).**
Uživatel má na produkci launchmailu ověřený setup: **Seznam e-mail zdarma pro
doménu** (`@mojedomena.cz` schránky) + odesílání přes Seznam SMTP. Seznam má
čisté IP + PTR + reputaci → doručitelnost bez vlastní infry. V homelab instanci
jen znovu vytvoř ten SmtpConfig (typ **smarthost**):
- [ ] Dashboard → SMTP → New → **Relay (smarthost)**:
      host `smtp.seznam.cz`, port `465` (SSL) nebo `587` (STARTTLS),
      username = plná adresa `neco@mojedomena.cz`, password = heslo schránky,
      From = tatáž adresa. Ověř doménu v launchmailu (DKIM) pro lepší inbox.
- [ ] Test: pošli sám sobě → dorazí do inboxu ✔

**Odesílání — ENDGAME: `direct` (vlastní ESP, 0 třetích stran).** Kód hotový
(Fáze 1–4: direct-MX, rate limity, greylist retry, bounce handling, warm-up,
deliverability konzole). Poslední krok je **egress node** (host s PTR + portem
25 — kamarádův server, nebo levná VPS): kompletní návod + skript + compose v
**`docs/mail-egress-node.md`**, `scripts/egress-node-setup.sh`,
`compose/mail-egress/`. Pak v launchmail UI vytvoř **Direct** SmtpConfig, publikuj
záznamy z Deliverability panelu, a Seznam nech jako fallback.

**Monitoring:**
```bash
cd /srv/homelab/compose/monitoring && sudo docker compose up -d
```
- [ ] `http://homelab:3001` → založ Kuma admin → přidej monitory: TCP `shared-postgres:5432`, TCP `smtp:587`, HTTP tvoje weby (až budou), **Push monitor „backup"** (interval 90000 s ≈ 25 h) → URL si zkopíruj.

## Fáze 6 — Zálohy (~30 min) — NEPŘESKAKUJ

Kompletní postup v `docs/backups-restore.md`. Zkráceně:
- [ ] USB SSD: `mkfs.ext4 -L BACKUP`, fstab s `nofail`, mount na `/mnt/backup`.
- [x] R2: bucket `homelab-backups` + API token hotové a ověřené (2026-07-11); config připraven v `secrets/rclone.conf`. Na serveru už jen:
  ```bash
  sudo install -m 600 -D /srv/homelab/secrets/rclone.conf /root/.config/rclone/rclone.conf
  sudo rclone lsd r2:    # vypíše homelab-backups → funguje
  ```
- [ ] Kuma push URL vlož do `scripts/backup.sh` (`KUMA_PUSH_URL=`).
- [ ] Instalace + první běh:
  ```bash
  sudo cp /srv/homelab/scripts/backup.sh /usr/local/bin/homelab-backup.sh && sudo chmod +x /usr/local/bin/homelab-backup.sh
  sudo cp /srv/homelab/scripts/systemd/backup.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now backup.timer
  sudo /usr/local/bin/homelab-backup.sh          # první záloha ručně
  rclone ls r2:homelab-backups                   # vidíš soubory? ✅
  ```

## Fáze 7 — Smoke test (2 min)

```bash
sudo bash /srv/homelab/scripts/smoke-test.sh
```
Vše ✔? **Server je hotový.** Něco ✘? Oprav, než pokračuješ.

## Fáze 8 — První projekt

Postupuj podle `docs/new-project-recipe.md`. Doporučené pořadí: nejdřív něco malého (statická vizitka — ověří celou cestu DNS→tunnel→Traefik), pak LeadCRM, pak zbytek. Migrace ze Supabase: `docs/migrate-from-supabase.md`.

---

## Provoz (rituály)

| Kdy | Co |
|---|---|
| denně | nic — vše automatické (zálohy, security updates, monitoring) |
| týdně | mrkni na Kuma dashboard + `df -h /` (disk pod 80 %) |
| měsíčně | `sudo apt update && sudo apt full-upgrade` → případný reboot; Dokploy update (UI); `cd /srv/homelab/compose/* && sudo docker compose pull && sudo docker compose up -d` |
| kvartálně | **restore drill** (`docs/backups-restore.md`) + `sudo smartctl -a /dev/nvme0` (zdraví disku) |

## Cheatsheet

```bash
ssh anakin@homelab                                  # přes Tailscale odkudkoli
sudo docker ps                                      # co běží
sudo docker logs -f <kontejner>                     # logy
sudo docker exec -it shared-postgres psql -U postgres   # DB konzole
/srv/homelab/scripts/newdb.sh <projekt>             # nová databáze
/srv/homelab/scripts/newdb.sh list                  # seznam databází
sudo systemctl status cloudflared tailscaled        # tunel + tailscale
sudo journalctl -u backup.service -n 50             # jak dopadla záloha
ssh -L 5432:localhost:5432 anakin@homelab           # DB z notebooku (TablePlus na localhost:5432)
```
