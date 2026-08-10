# Síť: Cloudflare Tunnel, domény, Tailscale

## Princip

- **Veřejný provoz** (weby, API): Cloudflare Tunnel → Traefik `:80` → aplikace. Žádný otevřený port, žádný port-forwarding na routeru. HTTPS certifikáty řeší Cloudflare edge. Výjimkou jsou níže popsané custom-domain hosty dostupné jen přes Tailscale; jejich certifikát vzniká přes DNS-01.
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

## 6) Freio operator hosty a Postiz Access

`outreach.freio.cz` a `posty.freio.cz` nejsou Cloudflare Tunnel hosty. Jejich
Cloudflare DNS je neproxované A na Tailscale IP serveru a veřejný internet tuto
adresu neroutuje. Vstup na TCP/443 vlastní `tailscale serve`, které přeposílá
raw TLS do lokálně bindnutého `127.0.0.1:9443`:

```text
Tailnet device → Tailscale Serve :443 → 127.0.0.1:9443
              → freio-private-proxy → Freio / Content Studio
```

Zdroj pravdy:

- `compose/freio-private-proxy/` — privátní Traefik proxy a exact-host routy;
- `scripts/freio-private-tailnet.sh` + systemd unit — idempotentní Serve config;
- `freio-private-tailnet-check.timer` — každých 15 minut ověří a případně
  idempotentně obnoví proxy i Serve mapu, potom zkontroluje platné HTTPS a
  očekávané odpovědi `outreach`/`posty` i aktivní privátní Postiz route;
- `compose/traefik/freio-private-hosts.yml` — fail-closed veřejný guard (403),
  kdyby někdo zkusil Host hlavičku přes hlavní Traefik;
- `traefik-dynamic-postiz.yml` — verzionovaný Postiz origin pro Cloudflare
  Access; `/uploads` bypass a owner-only pravidla spravuje
  `scripts/cf-access-postiz.py` na Cloudflare edge;
- `scripts/freio-private-cert-renew.sh` + timer — týdenní DNS-01 renewal check.
- `docs/freio-private-tailnet-runbook.md` — validace, instalace, acceptance a
  návrat na předchozí commit bez práce s produkčními secrets.

Certifikát má SAN `outreach.freio.cz`, `posty.freio.cz`, `postiz.freio.cz` a
`postiz-admin.freio.cz`, používá omezený Cloudflare DNS token a data jsou root-only v
`/etc/dokploy/traefik/dynamic/freio-private-certs`. Kontrola:

```bash
sudo freio-private-tailnet status
systemctl status freio-private-tailnet.service freio-private-tailnet-check.timer \
  freio-private-cert-renew.timer
curl -I https://outreach.freio.cz        # jen ze zařízení v Tailnetu
```

`freio.cz`, `www.freio.cz` a `api.freio.cz` musí zůstat veřejné.
`postiz.freio.cz` používá hybridní model schválený 10. 8. 2026. Zařízení se
správným split DNS dostanou privátní plnou aplikaci přes Tailnet. Prohlížeč,
který split DNS obejde přes DoH, skončí na Cloudflare Access e-mail OTP a projde
jen s jedním z owner e-mailů. `test.freio.cz` byl 7. 8. 2026 odstraněný z DNS i
tunnel ingressu.

### Aktivní Postiz Access + `/uploads` bypass

Audit 7. 8. ověřil, že všech 1 303 referencí v naplánované queue používá 701
unikátních URL pod `/uploads`. Cloudflare Access má pro tento prefix veřejný
bypass, aby sociální sítě média načetly. Zbytek hostu je owner-only Access
aplikace s e-mail OTP a 24hodinovou relací. Verzionovaný origin je
`traefik-dynamic-postiz.yml`; checker vyžaduje jeho byte-for-byte shodu s
aktivním `/etc/dokploy/traefik/dynamic/postiz.yml` a z veřejného DNS ověřuje
Access login redirect bez přihlášení.

Plná Postiz route je nadále i v privátní proxy a certifikát zahrnuje
`postiz.freio.cz` i DNS-only Tailnet fallback `postiz-admin.freio.cz`. Tailscale DNS používá
restricted nameserver `100.111.188.8` pro přesný suffix `postiz.freio.cz`;
`homelab-dns` vrací jen privátní A a žádnou veřejnou AAAA. Fallback admin host
zůstává čistě Tailnet-only. Pro DoH prohlížeče je primární fallback Cloudflare
Access. Produkční acceptance ověřuje owner login, privátní API, Access redirect
bez session a veřejné načtení `/uploads`. OAuth reconnect se ověřuje
kontrolovaně při další rotaci integrace.

## 7) Příjem e-mailů na doméně (volitelné, zdarma)

Cloudflare → doména → **Email → Email Routing**: `info@domena.cz` → forward na Gmail. CF přidá MX záznamy samo. (Odesílání řeší SMTP služba — viz `compose/smtp/`.)

## Bezpečnostní zásady

1. Dokploy panel, Kuma, SSH — nikdy nepublikovat přes tunel. Jen Tailscale.
2. Kdybys někdy potřeboval panel veřejně (nechtěj), jedině za Cloudflare Access (Zero Trust login).
3. DB port 5432 je bindnutý jen na `127.0.0.1` — z notebooku přes `ssh -L`, nikdy veřejně.
