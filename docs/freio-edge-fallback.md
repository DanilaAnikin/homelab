# Freio Cloudflare edge fallback

Tento slice připravuje druhou, na Homelabu nezávislou failure domain pro
`freio.cz` a `www.freio.cz`. Cloudflare Worker stojí před existujícím proxied
DNS/Tunnel originem. Zdravou odpověď předá beze změny; při výjimce, timeoutu
nebo HTTP `500`–`504` vrátí jen pro HTML navigaci secretless statickou stránku.
API, Next assety, nejednoznačně kódované cesty a zápisové metody vždy selžou
uzavřeně s JSON `503`. Worker nikdy neopakuje origin request.

## Aktuální stav: live od 12. 8. 2026

Worker `freio-edge-fallback` je v produkci na jediné verzi
`cc9c3b10-e0b4-48f6-8bd6-71381e2c0606` se 100 % trafficu. Stažený live modul
má SHA-256
`068a073b66daa52ea7925c976c848749057f3627b96aca167368058375e538a9`, tedy
přesně stejný hash jako verzovaný `worker.mjs`. `workers.dev` i preview URL
jsou vypnuté, Worker nemá bindings a drží compatibility flag
`global_fetch_private_origin`.

Aktivní jsou přesně dvě route bez DNS změny:

- `freio.cz/*` → route ID `c02122f4a1dd491ca60b32c6d6d8fd26`;
- `www.freio.cz/*` → route ID `779faf323c654921984f044e2f560bc8`.

Obě mají `request_limit_fail_open=true`: pokud účet běží na Workers Free,
při vyčerpání denního limitu zdravý origin zůstane dostupný napřímo. Edge
health pak ztratí svou hlavičku a minutový host monitor vyvolá
`notify-failure@freio-public-failover-check.service`. Root-owned marker
`/etc/freio-public-failover/edge-enabled` je aktivní a monitor je ve stavu
`primary`.

Live canary na obou hostnamech ověřil zdravý passthrough, HTML fallback pro
origin `502`, API fail-closed `503`, POST fail-closed bez replaye, čtyřsekundový
timeout a automatický recovery. Každý drill request vytvořil přesně jeden
origin attempt. Počáteční Workers metriky po release byly 176 requests,
0 runtime errors, 130 subrequests a CPU p99 1,683 ms.

Workers API hlásí `usage_model=standard`, ale deployment token záměrně nemá
Billing oprávnění a subscription endpoint vrací `403`. Aktivace nevolala
žádný billing ani subscription write endpoint, takže nevytvořila nový placený
závazek; z API dostupného tomuto tokenu však nelze dokázat, zda účet neměl
Workers Paid už před releasem.

Soubor `wrangler.toml` nadále záměrně nemá route a vypíná `workers.dev` i
preview URL. Samotné nahrání zdroje proto nemůže připojit další hostname.
Repozitář neobsahuje Cloudflare account ID, zone ID, token, bindings ani jiný
secret; live route zůstávají samostatný provozní stav Cloudflare.

`global_fetch_private_origin` je explicitně připnutý: origin `fetch(request)`
musí obejít Worker route a jít k DNS/Tunnel originu. Bez tohoto interlocku by
budoucí změna na strictly-public fetch mohla request vrátit na Cloudflare
„front door“ a rekurzivně znovu spustit tentýž Worker.

## Kontrakt

- Jen `freio.cz` a `www.freio.cz`; neočekávaný host selže `503`.
- HTTP se před originem přesměruje `308` na HTTPS.
- `GET`/`HEAD /__freio-edge-health` vrací veřejné secretless JSON `200` bez
  kontaktování originu.
- Jeden a pouze jeden origin `fetch`, timeout 4 sekundy, žádný retry.
- Origin `500`–`504` nebo fetch exception:
  - HTML dokumentová navigace `GET`/`HEAD`: inline branded HTML `200`;
  - `/api`, `/_next`, ne-HTML a všechny ostatní metody: JSON `503`.
- Origin `505` i ostatní statusy mimo přesný interval se předávají beze změny.
- Percent-encoded, opakovaně lomítkované nebo jinak nejednoznačné pathy nikdy
  nedostanou fallback HTML; při poruše originu selžou `503`.
- Fallback nemá skripty, obrázky, externí assety, formuláře, databázi, Stripe,
  cookies ani secrets. Neloguje URL, query string, Cookie ani Authorization.
- Fallback odpovědi nesou `Cache-Control: no-store`, `Retry-After: 20`, přísné
  CSP a `X-Freio-Edge-Fallback: static-v1`.

## Ověření bez Cloudflare

```bash
cd /srv/homelab
node --test tests/freio_edge_fallback/worker.test.mjs
python3 -m unittest discover -s tests/freio_edge_fallback -p 'test_*.py' -v
```

Testy ověřují zdravý passthrough, každý status `500`–`504`, `505` passthrough,
exception, timeout, GET/HEAD HTML fallback, API/Next fail-closed, encoded pathy,
malformed URL, HTTP redirect, veřejný health endpoint a nulový replay zápisů.

## Cloudflare token

Vytvoř nový least-privilege API token omezený na příslušný účet a jedinou
zónu `freio.cz`:

- Account: `Workers Scripts Read` a `Workers Scripts Write`;
- Zone `freio.cz`: `Workers Routes Read` a `Workers Routes Write`;
- Zone `freio.cz`: `Zone Read`.

Cloudflare Dashboard může dvojice Read/Write zobrazit jako jedinou úroveň
`Edit`; výsledný token musí umět příslušné GET i PUT/POST/DELETE endpointy, ale
nesmí mít DNS Edit, Tunnel Edit, Account Settings ani přístup do jiných zón.

Token nepatří do repozitáře ani do argumentů procesů. Ulož ho do root-only
souboru (`0600`), načti ho do prostředí až uvnitř chráněného deploy procesu a
nikdy nevypisuj jeho hodnotu ani hash.

## Preflight a staged rollout

Následující názvy jsou zástupné; identifikátory načti z API a nevkládej je do
gitu. Všechny read kroky musí projít před prvním zápisem.

1. Ověř token a inventář:

   ```text
   GET /client/v4/user/tokens/verify
   GET /client/v4/zones?name=freio.cz
   GET /client/v4/accounts/{account_id}/workers/scripts
   GET /client/v4/zones/{zone_id}/workers/routes
   ```

   Zastav při neaktivním tokenu, jiné/neaktivní zóně nebo route překrývající
   `freio.cz/*` či `www.freio.cz/*`.

2. Spusť lokální testy a ulož SHA-256 `worker.mjs` a `wrangler.toml` do
   release evidence.
3. Nahraj script `freio-edge-fallback`, ale stále bez routes a bez
   `workers.dev`. Wrangler používá `PUT
   /accounts/{account_id}/workers/scripts/freio-edge-fallback` s multipart
   metadata obsahujícími `main_module=worker.mjs` a
   `compatibility_date=2026-08-12`; metadata musí zachovat compatibility flag
   `global_fetch_private_origin`. Následně explicitně drž subdoménu vypnutou:

   ```text
   POST /client/v4/accounts/{account_id}/workers/scripts/freio-edge-fallback/subdomain
   {"enabled":false,"previews_enabled":false}
   ```

   Znovu načti script metadata a subdomain stav; pokračuj jen při shodném
   hashi/verzi a obou hodnotách `false`.
4. Pro live preview nevytvářej produkční route. Použij jednorázovou Workers
   version preview URL jen po samostatném schválení; po testu preview opět
   zakaž.
5. Připoj nejprve www přes přesný request a ulož vrácené route ID:

   ```text
   POST /client/v4/zones/{zone_id}/workers/routes
   {"pattern":"www.freio.cz/*","script":"freio-edge-fallback","request_limit_fail_open":true}
   ```

   Proveď zdravý passthrough smoke a řízený origin-failure drill. Ověř HTML
   `200` s fallback hlavičkou, API `503`, jeden origin attempt a automatický
   návrat na primary.
6. Teprve po úspěšném drillu připoj apex stejným endpointem a tělem
   `{"pattern":"freio.cz/*","script":"freio-edge-fallback","request_limit_fail_open":true}`.
   Ulož druhé route ID a zopakuj stejné kontroly.
7. Po úspěšném postflightu obou routes zapni připravený host monitor a hned ho
   spusť. Marker vytvoř až po nasazení apexu, protože vyžaduje health endpoint
   na obou hostnamech:

   ```bash
   install -d -o root -g root -m 0755 /etc/freio-public-failover
   install -o root -g root -m 0644 /dev/null \
     /etc/freio-public-failover/edge-enabled
   systemctl start --wait freio-public-failover-check.service
   ```

   Monitor ověří `X-Freio-Edge-Fallback: health-v1` na secretless endpointu a
   výskyt `X-Freio-Edge-Fallback: static-v1` změní na alertovaný stav
   `edge-fallback`. Nasazovací token nemá Billing ani Notifications oprávnění
   a nemůže vytvořit placený závazek. Cloudflare-side usage policy proto není
   součástí tohoto release; minutový host monitor hlídá Worker health,
   runtime chyby i vyčerpání kvóty zvenčí.

## Rollback

Před změnou si ulož přesná ID obou nových routes. Rollback nevyžaduje změnu
DNS: smaž nejprve apex route a potom www route přes
`DELETE /zones/{zone_id}/workers/routes/{route_id}`. Ověř, že DNS CNAME stále
ukazuje na původní Tunnel, oba hosty vracejí primary bez fallback hlavičky a
žádná překrývající route nezůstala. Script smaž až později po retenčním okně;
odpojení routes je rychlejší a bezpečnější incident rollback.

Po odpojení obou routes odstraň přesný marker a spusť monitor; bez tohoto kroku
by správně hlásil chybějící Worker health jako incident:

```bash
rm -f -- /etc/freio-public-failover/edge-enabled
systemctl start --wait freio-public-failover-check.service
```

## Provozní hranice

Na Workers Free je denní limit 100 000 požadavků a 10 ms CPU na invocation;
limit se obnovuje v 00:00 UTC. Obě live route při jeho vyčerpání obejdou
Worker a zachovají zdravý origin, ale edge fallback pak nebude fungovat, dokud
se kvóta neobnoví. Na Workers Paid/Standard je v ceně 10 milionů requestů a
30 milionů CPU ms za měsíc; nad limit je cena 0,30 USD za milion requestů a
0,02 USD za milion CPU ms. Počáteční live provoz je hluboko pod oběma
envelopes, provider-side usage alert ale tento token neumí ověřit ani
vytvořit.

Minutový monitor chybu alertuje, ale spotřebu předem nepředpovídá. Worker
pokrývá pád Homelabu, jediného `cloudflared`, Traefiku a origin aplikace.
Nepokrývá výpadek Cloudflare jako celého poskytovatele; ten vyžaduje druhého
DNS/CDN vendora.
