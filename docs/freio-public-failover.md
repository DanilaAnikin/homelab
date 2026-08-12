# Freio public automatic failover

`freio.cz` a `www.freio.cz` procházejí přes dvě stejné bezsecretové gateway
instance na společném Traefiku:

1. gateway předá požadavek na `freio-xkgrrq:3000`, pokud primary odpoví bez
   serverové chyby;
2. při connection chybě, timeoutu nebo odpovědi `5xx` vrátí sama bezpečný
   statický režim.

File-provider konfigurace `compose/traefik/freio-public-failover.yml` má vyšší
prioritu než Dokployem generovaný router a vede provoz pouze do gateway.
Traefik obě instance aktivně kontroluje; u GET/HEAD smí při dial chybě jednou
zkusit druhou instanci, zápisový požadavek nikdy neopakuje.
Request-aware gateway zachytí už první odpověď `5xx`: veřejné GET/HEAD stránky
převede na statický web s HTTP 200, zatímco API, Next asset požadavky a všechny
zápisové metody vrátí `503`. Zápisový požadavek se nikdy neopakuje. Gateway
nemá žádný aplikační secret, databázové ani Stripe spojení a nic z požadavků
neloguje.

Cloudflare hlavička `CF-Visitor` je autoritativní pro původní schéma. Běžný
veřejný HTTP požadavek na allowlistovaný apex nebo `www`, včetně veřejného
`/healthz`, gateway ukončí `308`
na stejnou HTTPS cestu ještě před kontaktem s primary; nepodporovaný
`Expect: 100-continue` zůstává fail-closed `417`. `X-Forwarded-Proto` je jen
fallback pro jiný důvěryhodný proxy vstup; `CF-Visitor=https` má přednost,
aby interní HTTP spojení Cloudflare Tunnelu nevytvořilo redirect loop. Toto je
transportní ochrana za Cloudflare Tunnel ingress, nikoli náhrada firewallu;
přímý neautentizovaný přístup k origin portu nesmí být veřejně dostupný.
Před proxy na Next.js gateway ze stejného autoritativního schématu znovu
sestaví jediné `X-Forwarded-Proto`; konfliktní hodnota z posledního interního
HTTP hopu ani hodnota dodaná klientem se nepředává. Tím exact HTTPS machine
endpointy dostanou `https`, zatímco poškozený `CF-Visitor` jim HTTPS autoritu
neudělí.

U zápisu po timeoutu znamená `503` výsledek „stav není bezpečně potvrzen“:
gateway požadavek nikdy sama neopakuje, ale upstream jej mohl přijmout ještě
před ztrátou odpovědi. Klient nebo operátor proto musí použít původní
idempotency key a canonical reconciliation; nesmí odeslat nový pokus naslepo.

## Instalace

```bash
cd /srv/homelab/compose/freio-public-fallback
docker compose build --pull
docker compose up -d

install -o root -g root -m 0644 \
  /srv/homelab/compose/traefik/freio-public-failover.yml \
  /etc/dokploy/traefik/dynamic/freio-public-failover.yml
install -o root -g root -m 0755 \
  /srv/homelab/scripts/freio-public-failover-check.sh \
  /usr/local/sbin/freio-public-failover-check
install -o root -g root -m 0644 \
  /srv/homelab/scripts/systemd/freio-public-failover-check.service \
  /srv/homelab/scripts/systemd/freio-public-failover-check.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl start freio-public-failover-check.service
systemctl enable --now freio-public-failover-check.timer
```

## Povinný maintenance gate

Hlavní službu nikdy neškáluj na nulu, dokud neprojde:

```bash
systemctl start --wait freio-public-failover-check.service
docker inspect freio-public-gateway-a --format '{{.State.Health.Status}}'
docker inspect freio-public-gateway-b --format '{{.State.Health.Status}}'
```

Při plánovaném testu škáluj primary na nulu až po tomto preflightu. Hned první
požadavek musí na `freio.cz` i `www.freio.cz` vracet HTTP 200, hlavičku
`X-Freio-Fallback: static-v1` a text `Záložní režim je aktivní`. API musí
vracet 503. Primary ihned vrať na jednu repliku a čekej na Docker health i
zmizení fallback hlavičky.

## Monitoring a incident

Timer běží každou minutu. Kromě konfigurace, obou gateway a HTTPS route
ověřuje pro apex i `www` přesnou odpověď `HTTP 308` se stejnou HTTPS
`Location`. Po aktivaci Cloudflare edge vrstvy přítomnost root-only markeru
`/etc/freio-public-failover/edge-enabled` navíc vynutí secretless health check
Workeru na obou hostnamech. Veřejná hlavička
`X-Freio-Edge-Fallback: static-v1` se zaznamená jako samostatný stav
`edge-fallback`, ne jako zdravý primary. Exit 1 znamená rozbitou
konfiguraci/zálohu, redirect, Worker health nebo nedostupný public edge. Exit 2
vznikne pouze při přechodu z primary do lokálního nebo edge fallbacku, aby
spustil jediný Telegram alert; další minuty fallbacku se zapisují do journalu
bez opakovaného spamu. Návrat na primary se rovněž zapíše do strukturovaného
výstupu.

Fallback není náhrada host-level disaster recovery: výpadek celého Homelabu,
Traefiku nebo Cloudflare Tunnelu vyžaduje samostatný druhý origin/edge vrstvu.
