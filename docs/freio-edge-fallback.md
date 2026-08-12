# Freio Cloudflare edge fallback

Tento slice připravuje druhou, na Homelabu nezávislou failure domain pro
`freio.cz` a `www.freio.cz`. Cloudflare Worker stojí před existujícím proxied
DNS/Tunnel originem. Zdravou odpověď předá beze změny; při výjimce, timeoutu
nebo HTTP `500`–`504` vrátí jen pro HTML navigaci secretless statickou stránku.
API, Next assety, nejednoznačně kódované cesty a zápisové metody vždy selžou
uzavřeně s JSON `503`. Worker nikdy neopakuje origin request.

## Aktuální stav: default-off

Soubor `wrangler.toml` záměrně nemá žádnou route a vypíná `workers.dev` i
preview URL. Samotné `wrangler deploy` tedy nesmí připojit veřejný hostname.
Tento repozitář neobsahuje Cloudflare account ID, zone ID, token, bindings ani
jiný secret. Produkční route se připojuje až samostatně po preflightu a
schváleném failure drillu.

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
   {"pattern":"www.freio.cz/*","script":"freio-edge-fallback"}
   ```

   Proveď zdravý passthrough smoke a řízený origin-failure drill. Ověř HTML
   `200` s fallback hlavičkou, API `503`, jeden origin attempt a automatický
   návrat na primary.
6. Teprve po úspěšném drillu připoj apex stejným endpointem a tělem
   `{"pattern":"freio.cz/*","script":"freio-edge-fallback"}`. Ulož druhé
   route ID a zopakuj stejné kontroly.
7. Nastav alert na výskyt `X-Freio-Edge-Fallback: static-v1`, origin `5xx`,
   Worker exception a spotřebu Workers kvóty.

## Rollback

Před změnou si ulož přesná ID obou nových routes. Rollback nevyžaduje změnu
DNS: smaž nejprve apex route a potom www route přes
`DELETE /zones/{zone_id}/workers/routes/{route_id}`. Ověř, že DNS CNAME stále
ukazuje na původní Tunnel, oba hosty vracejí primary bez fallback hlavičky a
žádná překrývající route nezůstala. Script smaž až později po retenčním okně;
odpojení routes je rychlejší a bezpečnější incident rollback.

## Provozní hranice

Workers Free má denní limit požadavků; fallback route musí mít před produkcí
potvrzenou kapacitu nebo Workers Paid a alert na kvótu. Tento Worker pokrývá
pád Homelabu, jediného `cloudflared`, Traefiku a origin aplikace. Nepokrývá
výpadek Cloudflare jako celého poskytovatele; ten vyžaduje druhého DNS/CDN
vendora.
