# Freio private Tailnet ingress

Tento runbook je zdroj pravdy pro nasazení a návrat privátních operator hostů
`outreach.freio.cz`, `posty.freio.cz` a `postiz-admin.freio.cz` a pro privátní
Tailnet cestu k `postiz.freio.cz`. Postiz má navíc vědomě schválenou veřejnou
Cloudflare Access bránu pro owner e-maily a veřejný `/uploads` bypass. Produkční
secrets, certifikát ani jeho privátní klíč nejsou součástí repozitáře.

## Verzionované artefakty

- `compose/freio-private-proxy/docker-compose.yml`
- `compose/freio-private-proxy/dynamic.yml`
- `compose/traefik/freio-private-hosts.yml`
- `traefik-dynamic-postiz.yml`
- `scripts/freio-private-tailnet.sh`
- `scripts/freio-private-cert-renew.sh`
- `scripts/systemd/freio-private-tailnet.service`
- `scripts/systemd/freio-private-tailnet-check.service`
- `scripts/systemd/freio-private-tailnet-check.timer`
- `scripts/systemd/freio-private-cert-renew.service`
- `scripts/systemd/freio-private-cert-renew.timer`

## Povinný preflight

Nasazuje se jen konkrétní zkontrolovaný Git commit z čistého checkoutu. Před
kopírováním musí projít:

```bash
bash -n scripts/freio-private-tailnet.sh scripts/freio-private-cert-renew.sh
docker compose -f compose/freio-private-proxy/docker-compose.yml config --quiet
systemd-analyze verify scripts/systemd/freio-private-*.service \
  scripts/systemd/freio-private-*.timer
```

Všechny Traefik YAML soubory se musí načíst strict YAML parserem. Skripty se
instalují jako `root:root 0755`, units a YAML jako `root:root 0644`. Stávající
produkční artefakty se před změnou kopírují do nového root-only adresáře s
časovým razítkem; rollback nikdy nesmí spoléhat na neznámý stav worktree.

## Produkční mapování

| Zdroj | Cíl |
|---|---|
| `compose/freio-private-proxy/*` | `/srv/homelab/compose/freio-private-proxy/` |
| `compose/traefik/freio-private-hosts.yml` | `/srv/homelab/compose/traefik/` a `/etc/dokploy/traefik/dynamic/freio-private-hosts.yml` |
| `traefik-dynamic-postiz.yml` | `/srv/homelab/traefik-dynamic-postiz.yml` a `/etc/dokploy/traefik/dynamic/postiz.yml` |
| `scripts/freio-private-tailnet.sh` | `/srv/homelab/scripts/` a `/usr/local/sbin/freio-private-tailnet` |
| `scripts/freio-private-cert-renew.sh` | `/srv/homelab/scripts/` a `/usr/local/sbin/freio-private-cert-renew` |
| `scripts/systemd/*` | `/srv/homelab/scripts/systemd/` a `/etc/systemd/system/` |

Po atomickém nahrazení souborů následuje `systemctl daemon-reload`, restart
`freio-private-tailnet.service` a ruční spuštění
`freio-private-tailnet-check.service`. Timery musí zůstat enabled/active.

## Acceptance

`freio-private-tailnet check` failuje, pokud:

- privátní proxy neposlouchá výhradně na `127.0.0.1:9443`;
- Serve mapa není přesně jediný TCP/443 → `127.0.0.1:9443`, `Web` není prázdný,
  `Foreground` existuje nebo `AllowFunnel` není prázdný (Funnel je zakázaný);
- privátní outreach/posty/Postiz routa nevrátí očekávaný stav;
- hlavní Traefik nevrátí pro Tailnet-only operator hosty HTTP 403 na HTTP i HTTPS;
- veřejný Postiz root nevrátí přes veřejný DNS očekávanou Cloudflare Access
  login bránu nebo syntetická neexistující `/uploads` URL neprojde Access
  bypassem až k origin 404;
- aktivní guard nebo Postiz origin policy není byte-for-byte shodná s verzionovaným
  zdrojem na serveru.

Navíc se z Tailnet zařízení ověří HTTP 200 pro outreach, HTTP 401 a health 200
pro posty a přesměrování UI Postiz. Z veřejné sítě musí outreach, posty a
postiz-admin zůstat nedostupné. Postiz root musí skončit na owner-only
Cloudflare Access loginu; veřejnou výjimkou je `/uploads` popsaný v
`docs/networking.md`.

## Rollback

Rollback obnoví výhradně celý předchozí auditovaný artifact set z konkrétního
Git commitu nebo z jeho root-only přednasazovací kopie. Po obnově se opakuje
`daemon-reload`, restart služby a celý acceptance blok. Certifikát, DNS token a
Tailscale node key se při běžném rollbacku nemění ani nekopírují.
