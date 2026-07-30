# Síť: Cloudflare Tunnel, domény, Tailscale

## Princip

- **Veřejný provoz** (weby, API): Cloudflare Tunnel → Traefik `:80` → aplikace. Žádný otevřený port, žádný port-forwarding na routeru. HTTPS certifikáty řeší Cloudflare edge — Let's Encrypt na serveru vůbec nepotřebuješ.
- **Admin provoz** (SSH, Dokploy `:3000`, Kuma `:3001`): jen přes **Tailscale**. Panel NIKDY nepublikuj tunelem.

## 1) Onboarding domény na Cloudflare (jednou per doména, ~5 min)

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Add site** → doména → **Free** plan.
2. U registrátora (kde máš doménu koupenou) přepni **nameservery** na ty dva od Cloudflare.
3. Počkej na aktivaci (mail od CF, obvykle minuty až hodiny).
4. SSL/TLS → Overview → mode **Full**; Edge Certificates → **Always Use HTTPS: On**.

## 2) Tunnel (jednou, ~10 min)

1. [Zero Trust](https://one.dash.cloudflare.com) → **Networks → Tunnels → Create a tunnel** → typ **Cloudflared** → název `homelab`.
2. Zkopíruj token (`eyJ…`) a na serveru:
   ```bash
   sudo cloudflared service install <TOKEN>
   systemctl status cloudflared    # active (running), connector v dashboardu zelený
   ```

## 3) Napojení domény na tunel (per doména, ~3 min)

V detailu tunelu → **Public Hostnames → Add**:

| Hostname | Service |
|---|---|
| `domena.cz` | `http://localhost:80` |
| `*.domena.cz` | `http://localhost:80` |

⚠️ U **wildcardu** Cloudflare DNS záznam nevytvoří sám — přidej ručně:
DNS → Records → `*` → **CNAME** → `<TUNNEL_ID>.cfargotunnel.com` → Proxied ✅
(Root hostname si CNAME vytvoří automaticky.)

Náš tunel `homelab` (vytvořen 2026-07-11):
- Tunnel ID: `<TUNNEL_ID — viz secrets/homelab.conf>`
- wildcard CNAME cíl: `<TUNNEL_ID — viz secrets/homelab.conf>.cfargotunnel.com`
- token: `secrets/cloudflared-token.txt` (na serveru `/srv/homelab/secrets/`)

Od té chvíle **jakákoli subdoména** téhle domény teče na server — nové appky už řešíš jen v Dokploy, do Cloudflare nesaháš.

## 4) Doména u aplikace v Dokploy

App → **Domains → Add Domain**:
- Host: `app.domena.cz`, Path `/`, Container Port: port appky (Next.js `3000`)
- **HTTPS: OFF** (TLS terminuje Cloudflare; Traefik dostává čisté HTTP z tunelu)

Traefik si podle Host hlavičky sám najde správný kontejner. Hotovo — web jede na `https://app.domena.cz`.

## 5) Tailscale (admin odkudkoli)

- Server už je připojený z bootstrapu (`tailscale up --ssh`).
- [Admin console](https://login.tailscale.com/admin/machines): u `homelab` → **Disable key expiry** (jinak za pár měsíců vyprší auth).
- Notebook/mobil: nainstaluj Tailscale, přihlas stejným účtem.
- Pak odkudkoli na světě: `ssh anakin@homelab`, `http://homelab:3000` (Dokploy), `http://homelab:3001` (Kuma).
- SSH config na notebooku (`~/.ssh/config`), ať funguje i `ssh homelab`:
  ```
  Host homelab
      HostName homelab      # MagicDNS jméno (falls back: Tailscale IP 100.x.y.z)
      User anakin
  ```

## 6) Příjem e-mailů na doméně (volitelné, zdarma)

Cloudflare → doména → **Email → Email Routing**: `info@domena.cz` → forward na Gmail. CF přidá MX záznamy samo. (Odesílání řeší SMTP služba — viz `compose/smtp/`.)

## Bezpečnostní zásady

1. Dokploy panel, Kuma, SSH — nikdy nepublikovat přes tunel. Jen Tailscale.
2. Kdybys někdy potřeboval panel veřejně (nechtěj), jedině za Cloudflare Access (Zero Trust login).
3. DB port 5432 je bindnutý jen na `127.0.0.1` — z notebooku přes `ssh -L`, nikdy veřejně.
