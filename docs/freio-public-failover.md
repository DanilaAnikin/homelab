# Freio public automatic failover

`freio.cz` a `www.freio.cz` mají dvě oddělené runtime větve na společném
Traefiku:

1. `freio-xkgrrq:3000` — hlavní Dokploy/Swarm aplikace;
2. `freio-public-fallback:8080` — warm, bezsecretový, read-only statický web.

File-provider konfigurace `compose/traefik/freio-public-failover.yml` má vyšší
prioritu než Dokployem generovaný router. Traefik kontroluje oba upstreamy po
dvou sekundách. Nedostupný primary automaticky přepne veřejné GET/HEAD stránky
na statický web. API, Next asset požadavky a všechny zápisové metody záloha
vracejí `503`; nemůže tedy zapisovat do databáze, volat Stripe ani odesílat
e-maily.

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
docker inspect freio-public-fallback --format '{{.State.Health.Status}}'
```

Při plánovaném testu škáluj primary na nulu až po tomto preflightu. Do tří
sekund musí `freio.cz` i `www.freio.cz` vracet HTTP 200, hlavičku
`X-Freio-Fallback: static-v1` a text `Záložní režim je aktivní`. API musí
vracet 503. Primary ihned vrať na jednu repliku a čekej na Docker health i
zmizení fallback hlavičky.

## Monitoring a incident

Timer běží každou minutu. Exit 1 znamená rozbitou konfiguraci/zálohu nebo
nedostupný public edge. Exit 2 znamená, že veřejný web sice funguje, ale jede
z fallbacku nebo primary nemá zdravou repliku. Oba stavy spouštějí Freio
observability notifier.

Fallback není náhrada host-level disaster recovery: výpadek celého Homelabu,
Traefiku nebo Cloudflare Tunnelu vyžaduje samostatný druhý origin/edge vrstvu.
