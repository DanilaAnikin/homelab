# Freio prospecting worker V1

Tento worker připravuje influencer/ambassador kandidáty pro Freio, ale sám nikoho nekontaktuje, nikoho nezařazuje do rozesílky a neověřuje e-mail jako schválený k oslovení. Má dvě procesně i credentialově oddělené části:

1. `discover.py` přijme striktní JSON z výzkumu a může udělat pouze nedůvěryhodný preview fetch. Každého přeživšího kandidáta uloží do samostatného canonical manifestu v `ready`; jeden konflikt tedy nikdy nezablokuje sousední lead.
2. `submit.py` manifest atomicky claimne pod oddělenou Unix identitou, znovu sám načte veřejný zdroj, ověří přesnou adresu, vytvoří trusted receipt a teprve potom může jeden kandidát = jeden request předat internímu Freio intake endpointu. Síťový POST je možný pouze s explicitním `--send` a transportním HMAC credentialem.
3. `discover.py --housekeeping` a `submit.py --housekeeping` jsou oddělené offline retention/recovery větve bez DNS/HTTP. Oba šestihodinové timery jsou marker-disabled a musí být při budoucí aktivaci spuštěny dřív než discovery/submit.

## Bezpečnostní hranice

- Discovery služba nikdy nemá submit HMAC klíč. Pro Claude CLI má pouze discovery-only Anthropic API key a nemůže zapisovat do `processing`, `processed` ani submit quarantine.
- Submit služba nedostává Anthropic API key a nikdy nespouští LLM.
- Transportní HMAC a stabilní `identity-index-hmac` jsou dva různé 64znakové lowercase-hex secrety. Discovery nemá ani jeden; submit housekeeping dostane pouze identity credential přes `LoadCredential`, discovery housekeeping žádný credential.
- LLM výstup je nedůvěryhodný: parser odmítá neznámá pole, více než 30 kandidátů, ne-HTTPS URL, neplatné typy i nekonzistentní handle/channel.
- Discovery preview nikdy nevytváří autorizující receipt. Submit identita ignoruje veškerá případná tvrzení discovery procesu a bezprostředně před HMAC podpisem provede vlastní deterministický refetch každého kandidáta.
- Zdroj se načítá s celkovým limitem 10 sekund včetně DNS, nejvýše 3 redirecty a finálním tělem nejvýše 1 MiB. URL je před fetch/podpisem idempotentně srovnána s WHATWG kontraktem (host musí být již canonical ASCII/punycode; Unicode host se fail-closed odmítne kvůli rozdílu IDNA2003/UTS #46, cesta/query se percent-encoduje, odstraní se i percent-encoded dot-segmenty a výsledek má nejvýše 2000 UTF-16 jednotek). Přijat je přesně HTTP 200 a pouze `text/html` nebo `text/plain`; komprese, nejednoznačná délka a duplicitní kritické hlavičky se odmítají.
- Každý hostname se před každým requestem znovu DNS-resolvuje. Pokud kterákoli A/AAAA odpověď není globální veřejná adresa, request se odmítne. Spojení je připnuté na zvalidovanou IP, ale TLS/SNI se ověřuje proti původnímu hostname.
- E-mail se přijme jen při přesné shodě s adresou v parsovaném DOM textu nebo `mailto:` na výsledné zdrojové stránce; `script/style/noscript/template/svg` se ignorují. Nejde o browser/CSS rendering důkaz. Obfuskované, odhadnuté a přesměrováním změněné údaje se nepřijmou.
- Samotný intake vytváří jen neověřený kandidátský kontakt určený k ručnímu review. Nezapíná partner automation ani nevytváří zprávu.
- Trusted receipt vytvořený výhradně submit identitou obsahuje requested/final URL, fetchedAt, HTTP 200, redirect count, media type/charset, přesnou délku finálního těla, body SHA-256, hash přesně nalezeného e-mailu, match method, per-evidence SHA-256 a verze fetch/network policy. Raw stránka se neukládá ani neposílá. Canonical intake payload, receipt set i obohacené items mají vlastní SHA-256.
- Self-contained canonical JSON včetně všech fetch receipts má nejvýše 96 KiB a celý je součástí body SHA/HMAC. Receiver tedy může evidence receipt atomicky uložit spolu s intake výsledkem.
- Spool používá atomické přechody `ready -> processing -> processed hash-only receipt` nebo per-candidate quarantine. Canonical request bytes se uloží před prvním POSTem. Timeout/DNS/network/408/425/429/5xx mají exponenciální, nejvýše pět pokusů a 24hodinové okno; request retry je byte-for-byte stejný se stejným `Idempotency-Key`. Po nejasném vyčerpání vznikne hash-only `uncertain` tombstone a vyžaduje DB/operator reconciliation. Nikdy se tiše nesmaže request, který mohl commitnout vzdáleně.
- Dlouhodobý index neobsahuje handle, e-mail ani URL. Obsahuje jen domain-separated keyed HMAC identity (social channel+normalizovaný handle, normalizovaný e-mail; source URL jen fallback bez social/e-mailu), receipt/research hash a stav. První přijatá identita vyhrává; rediscovery se před POSTem přeskočí a změny se dělají ručně v dashboardu. Transport secret lze rotovat samostatně, identity secret musí zůstat stabilní. Jeho rotace vyžaduje vypnout submit a DB-backed rebuild celého indexu; automatický rekey z hashů není možný.
- Po 2xx se raw manifest i request smažou a zůstane jen omezený hash-only receipt s explicitní 30denní expirací. `ready`, deferred a terminal quarantine raw data i opuštěné atomic temp soubory mají 72hodinovou TTL. Dva nezávislé housekeeping timery ji vymáhají i při vypnuté discovery/submit službě. Jedinou záměrnou výjimkou je aktivní nejasný request za otevřeným global circuit/journalem, který se zachová do explicitního operator rozhodnutí.
- Claude CLI je vždy spouštěn s explicitní lokální hranicí `--claude-auth-mode api-key`; jediný credential se do minimálního allowlistu prostředí procesu mapuje jako oficiální `ANTHROPIC_API_KEY`. Worker neodvozuje typ credentialu z hodnoty, nepodporuje v této službě OAuth token a key nikdy nevkládá do argv, promptu ani vlastního logu.
- Claude běží v auditované neinteraktivní konfiguraci shodné s B2B discovery: přesný model `claude-sonnet-5`, maximálně `1.00` USD na běh, `--permission-mode dontAsk`, `--safe-mode`, explicitní allowlist pouze `WebSearch,WebFetch` a strukturovaný JSON schema output. Prompt jde bounded stdin pipe, nikoli argv; plný schema kontrakt se po návratu znovu vynucuje lokálně.
- Claude CLI dostává `HOME` pouze v privátním systemd `RuntimeDirectory`; po každém ukončení discovery unitu systemd celý adresář odstraní (`RuntimeDirectoryPreserve=no`). Cache, logy ani metadata Claude CLI proto nezůstávají mimo 72h spool retention.
- 400/401/403/404/405/413/415, redirect/protocol drift nebo nevalidní 2xx otevřou hash-only global circuit, zastaví celý běh a zachovají aktuální i sousední kandidáty. `409` přesune jen jeden kandidát do `uncertain` a také zastaví běh.
- Textové limity používají stejné UTF-16 code units jako Zod/JavaScript; lone surrogate selže jako validace, nikoli runtime crash.
- Validační chyby nikdy neopakují nedůvěryhodné JSON klíče nebo hodnoty do stderr, quarantine metadata ani systemd journalu; neočekávaná pole se hlásí jen počtem.

## Stav V1: záměrně vypnuto

Repozitář obsahuje jen šablony. Nic je neinstaluje, nespouští ani nezapíná. Všechny čtyři služby mají pojistku `ConditionPathExists`; bez markerů níže neudělají nic. Discovery má hard budget 540 s (Claude nejvýše 240 s + bounded preview) pod 10min unit limitem. Submit má 210 s pro nejvýše deset 10s fetch + 10s POST párů pod 4min unit limitem. Skutečný outreach SEND timer do tohoto workeru nepatří.

**Nezapínat submit**, dokud produkční Freio neposkytuje samostatný interní HMAC endpoint se stejným canonical-request protokolem, ochranou proti replay (časové okno + unikátní nonce), konstantním porovnáním podpisu, tělem do 96 KiB a stejným atomickým intake kontraktem. Aktuální browser-admin endpoint není pro worker vhodný a worker se nesmí přihlašovat uživatelskou session.

## Soubory

- `scripts/freio-prospecting/discover.py` — discovery CLI a Claude adapter.
- `scripts/freio-prospecting/submit.py` — validate-only/explicit-send CLI.
- `scripts/freio-prospecting/freio_prospecting/` — validace, fetch, podpis a workflow.
- `scripts/freio-prospecting/schemas/` — schema nedůvěryhodného research výstupu a ověřené dávky.
- `scripts/freio-prospecting/prompts/discover-v1.txt` — research-only prompt bez oprávnění k mutacím.
- `scripts/systemd/freio-prospect-{discovery,submit}.{service,timer}` a odpovídající `*-housekeeping.{service,timer}` — disabled-by-default šablony.

## Offline ověření

Research JSON lze zpracovat bez Claude CLI:

```bash
python3 scripts/freio-prospecting/discover.py \
  --spool /tmp/freio-prospecting-spool \
  --input-json /path/to/research.json
```

Příkaz načítá veřejné evidence URL, ale nic neposílá Freio API. Kompletně bez síťového odesílání lze zkontrolovat už připravené dávky:

```bash
python3 scripts/freio-prospecting/submit.py \
  --spool /tmp/freio-prospecting-spool \
  --endpoint https://outreach.freio.cz/api/internal/growth-partners/prospect-intake \
  --validate-only
```

`--validate-only` nečte credential, neprovádí POST a nemění stav spoolu.

Testy jsou čistě lokální a veškerou síť mockují:

```bash
python3 -m unittest discover -s tests/freio_prospecting -v
```

## Budoucí instalace (bez zapnutí)

Následující kroky smí přijít až po code review. Pouhé zkopírování unitů služby nezapne:

```bash
sudo groupadd --system freio-prospecting
sudo useradd --system --gid freio-prospecting --home-dir /nonexistent --shell /usr/sbin/nologin freio-discovery
sudo useradd --system --gid freio-prospecting --home-dir /nonexistent --shell /usr/sbin/nologin freio-submit
sudo install -d -o root -g freio-prospecting -m 0750 /var/lib/freio-prospecting /var/lib/freio-prospecting/deferred /var/lib/freio-prospecting/quarantine
sudo install -d -o freio-discovery -g freio-prospecting -m 0770 /var/lib/freio-prospecting/ready
sudo install -d -o freio-discovery -g freio-prospecting -m 0700 /var/lib/freio-prospecting/deferred/untrusted /var/lib/freio-prospecting/quarantine/untrusted
sudo install -d -o freio-submit -g freio-prospecting -m 0700 /var/lib/freio-prospecting/processing /var/lib/freio-prospecting/processed /var/lib/freio-prospecting/state /var/lib/freio-prospecting/quarantine/claimed
sudo install -d -o root -g root -m 0700 /etc/freio-prospecting /etc/freio-prospecting/enabled
sudo install -o root -g root -m 0644 scripts/systemd/freio-prospect-*.service scripts/systemd/freio-prospect-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Do tohoto okamžiku se nevytváří žádný soubor v `/etc/freio-prospecting/enabled/`, nevolá se `systemctl enable` a neukládají se credentials. Root spool musí zůstat přesně bez group/other write; `Spool.ensure()` konfiguraci 0770 fail-closed odmítne.

Influencer discovery bezpečně znovupoužívá existující discovery-only Anthropic API key v `/etc/freio-b2b-discovery/anthropic-api-key` s vlastníkem `root:root` a režimem `0600`; nevytváří druhou kopii ani OAuth token. PID 1 ho zpřístupní pouze discovery procesu jako privátní systemd credential `anthropic-api-key`. Obsah se nesmí vypisovat do shellu, journalu ani validačních výstupů. Transport a identity klíče jsou doslovné ASCII výstupy dvou samostatných `openssl rand -hex 32`; Freio env obsahuje stejných 64 znaků transport klíče bez newline. Soubory mají režim `0600` a nikdy nepatří do Gitu:

```bash
openssl rand -hex 32 | sudo tee /etc/freio-prospecting/submit-hmac >/dev/null
openssl rand -hex 32 | sudo tee /etc/freio-prospecting/identity-index-hmac >/dev/null
sudo chmod 0600 /etc/freio-prospecting/submit-hmac /etc/freio-prospecting/identity-index-hmac
```

`submit.env` obsahuje pouze necitlivou přesnou endpoint URL:

```ini
FREIO_PROSPECTING_ENDPOINT=https://outreach.freio.cz/api/internal/growth-partners/prospect-intake
```

## Budoucí řízené zapnutí

Po nasazení a otestování HMAC receiveru se každá vrstva zapíná zvlášť:

1. Vytvořit markery `enabled/discovery-housekeeping` a `enabled/submit-housekeeping`, ručně spustit oba housekeeping services a ověřit nulový/čekaný audit. Teprve potom zapnout jejich 6h timers; bez nich se discovery ani submit nesmí aktivovat.
2. Vytvořit pouze marker `enabled/discovery`, ručně spustit discovery service a zkontrolovat `ready`/deferred/quarantine po jednom kandidátovi.
3. Provést `submit.py --validate-only` a ručně zkontrolovat první kandidáty v dashboardu bez změny outreach automation.
4. Vytvořit marker `enabled/submit`, ručně spustit submit service a ověřit hash-only receipt v `processed` a nulový raw obsah po 2xx.
5. Teprve po acceptance checks lze zapnout discovery/submit timers. Master switch skutečného outreach odesílání zůstává mimo tento worker a vypnutý do samostatného schválení.

Oba housekeeping procesy běží pod existujícími oddělenými identitami: discovery cleanup nevidí submit `0700` adresáře ani credentialy, submit cleanup nevidí Anthropic API key ani discovery private adresáře. Žádný housekeeping proces neběží jako root a oba mají zakázanou IP síť (`RestrictAddressFamilies=AF_UNIX`).

## Operator reconciliation a global circuit

Nejasný receipt se rozhoduje až po kontrole DB/receiver logu. Oba příkazy drží stejný submit lock jako timer; `not-accepted` používá durable journal, bezpečně requeueuje dochovaný manifest a teprve potom odstraní tombstone:

```bash
python3 scripts/freio-prospecting/submit.py --spool /var/lib/freio-prospecting --identity-secret-file /run/credentials/.../identity-index-hmac --reconcile-accepted 'sha256:<64hex>'
python3 scripts/freio-prospecting/submit.py --spool /var/lib/freio-prospecting --identity-secret-file /run/credentials/.../identity-index-hmac --reconcile-not-accepted 'sha256:<64hex>'
```

Global circuit se smí odstranit až po opravě/ověření receiveru, secretu, routy a canonical parity. Odstranění nic neposílá; další submit běh znovu použije exact persisted body:

```bash
python3 scripts/freio-prospecting/submit.py --spool /var/lib/freio-prospecting --identity-secret-file /run/credentials/.../identity-index-hmac --clear-global-error
```

## HMAC receiver kontrakt

Canonical request jsou UTF-8 řádky bez koncového newline:

```text
freio-prospecting-v1
<unix timestamp>
<32 lowercase hex nonce>
POST
/api/internal/growth-partners/prospect-intake
<sha256 hex přesných body bytes>
```

Podpis je `HMAC-SHA256(secret, canonical_request)`, kde secret jsou ASCII bytes přesně 64 lowercase-hex znaků (nikoli hex-decode do 32 bytes), a posílá se jako `X-Freio-Prospecting-Signature: v1=<hex>`. Přesné další hlavičky jsou `X-Freio-Prospecting-Version`, `X-Freio-Prospecting-Timestamp`, `X-Freio-Prospecting-Nonce` a `X-Freio-Prospecting-Content-SHA256`; `Idempotency-Key` je receipt ID. Receiver musí podpis ověřit před parsováním JSON, odmítnout clock skew nad pět minut, atomicky rezervovat nonce a nikdy logovat podpis, body ani e-mail.

Request body je canonical UTF-8 JSON bez trailing newline a s přesnými top-level poli:

```text
{schemaVersion:"1", receipt:{algorithm,researchManifestSha256,intakePayloadSha256,evidenceSha256,signedItemsSha256,receiptId}, items:[...]}
```

Worker posílá přesně jeden item v requestu. Item má `idempotencyKey`, `prospect`, volitelný `emailCandidate` a povinný `fetchReceipt`. `requestedUrl`, `finalUrl` i email source musí už být exact canonical WHATWG-compatible URL; receiver odmítá jinou reprezentaci. Fetch receipt má přesně `requestedUrl`, `finalUrl`, `fetchedAt`, `observedAt`, `status`, `redirectCount`, `mediaType`, `charset`, `byteLength`, `bodySha256`, `matchedEmailSha256`, `matchMethod`, `fetcherVersion`, `networkPolicyVersion` a `evidenceSha256`. Úplný strojový kontrakt je v `scripts/freio-prospecting/schemas/intake-batch.schema.json`.

- `intakePayloadSha256` je hash canonical `{items}` po odstranění `fetchReceipt` z každého itemu.
- `evidenceSha256` je hash canonical pole všech `fetchReceipt` ve stejném pořadí.
- `signedItemsSha256` je hash canonical obohaceného `items`; `receiptId` je `sha256:<signedItemsSha256>`.
- Každé `fetchReceipt.evidenceSha256` je hash stejného objektu bez samotného pole `evidenceSha256`.
- Stejný `receiptId` a stejné body musí receiver idempotentně vrátit jako HTTP 200/replayed. Stejný klíč s jiným body je HTTP 409.
