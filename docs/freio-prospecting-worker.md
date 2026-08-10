# Freio prospecting worker V1

Tento worker připravuje influencer/ambassador kandidáty pro Freio, ale sám nikoho nekontaktuje, nikoho nezařazuje do rozesílky a neověřuje e-mail jako schválený k oslovení. Má dvě procesně i credentialově oddělené části:

1. `discover.py` přijme striktní JSON z výzkumu a může udělat pouze nedůvěryhodný preview fetch. Do `ready` uloží jen canonical candidate manifest bez evidence receipt a bez intake payloadu.
2. `submit.py` manifest atomicky claimne pod oddělenou Unix identitou, znovu sám načte každý veřejný zdroj, ověří přesnou adresu, vytvoří trusted receipt a teprve potom může payload podepsat a předat internímu Freio intake endpointu. Síťový POST je možný pouze s explicitním `--send` a HMAC credentialem.

## Bezpečnostní hranice

- Discovery služba nikdy nemá submit HMAC klíč. Má případně jen vlastní Claude credential a nemůže zapisovat do `processing`, `processed` ani submit quarantine.
- Submit služba nedostává Claude credential a nikdy nespouští LLM.
- LLM výstup je nedůvěryhodný: parser odmítá neznámá pole, více než 30 kandidátů, ne-HTTPS URL, neplatné typy i nekonzistentní handle/channel.
- Discovery preview nikdy nevytváří autorizující receipt. Submit identita ignoruje veškerá případná tvrzení discovery procesu a bezprostředně před HMAC podpisem provede vlastní deterministický refetch každého kandidáta.
- Zdroj se načítá s celkovým limitem 10 sekund včetně DNS, nejvýše 3 redirecty a finálním tělem nejvýše 1 MiB. Přijat je přesně HTTP 200 a pouze `text/html` nebo `text/plain`; komprese, nejednoznačná délka a duplicitní kritické hlavičky se odmítají.
- Každý hostname se před každým requestem znovu DNS-resolvuje. Pokud kterákoli A/AAAA odpověď není globální veřejná adresa, request se odmítne. Spojení je připnuté na zvalidovanou IP, ale TLS/SNI se ověřuje proti původnímu hostname.
- E-mail se přijme jen při přesné shodě s adresou ve viditelném textu nebo `mailto:` na výsledné zdrojové stránce. Obfuskované, odhadnuté a přesměrováním změněné údaje se nepřijmou.
- Samotný intake vytváří jen neověřený kandidátský kontakt určený k ručnímu review. Nezapíná partner automation ani nevytváří zprávu.
- Trusted receipt vytvořený výhradně submit identitou obsahuje requested/final URL, fetchedAt, HTTP 200, redirect count, media type/charset, přesnou délku finálního těla, body SHA-256, hash přesně nalezeného e-mailu, match method, per-evidence SHA-256 a verze fetch/network policy. Raw stránka se neukládá ani neposílá. Canonical intake payload, receipt set i obohacené items mají vlastní SHA-256.
- Self-contained canonical JSON včetně všech fetch receipts má nejvýše 96 KiB a celý je součástí body SHA/HMAC. Receiver tedy může evidence receipt atomicky uložit spolu s intake výsledkem.
- Spool používá atomické přechody nedůvěryhodného manifestu `ready -> processing -> processed` nebo `quarantine/claimed`. Discovery chyby jdou odděleně do `quarantine/untrusted`. Canonical request bytes se atomicky uloží v submit-only `processing` ještě před prvním POSTem. Při timeoutu/408/425/429/5xx zůstávají na místě a další běh odešle byte-for-byte stejné body a stejné `Idempotency-Key` bez nového fetch; nový je jen timestamp, nonce a HMAC. Validovaný remote success se před state rename zapíše do durable success markeru, takže crash recovery neposílá úspěšný request znovu.

## Stav V1: záměrně vypnuto

Repozitář obsahuje jen šablony. Nic je neinstaluje, nespouští ani nezapíná. Obě služby mají další pojistku `ConditionPathExists`; bez explicitních markerů níže neudělají nic. Discovery i idempotentní intake-submit timer používají `Persistent=true`, aby po budoucím zapnutí bezpečně dohnaly nejvýše jeden zmeškaný non-send běh. Skutečný `freio-outreach` SEND timer do tohoto workeru nepatří a jeho zákaz catch-upu se nemění.

**Nezapínat submit**, dokud produkční Freio neposkytuje samostatný interní HMAC endpoint se stejným canonical-request protokolem, ochranou proti replay (časové okno + unikátní nonce), konstantním porovnáním podpisu, tělem do 96 KiB a stejným atomickým intake kontraktem. Aktuální browser-admin endpoint není pro worker vhodný a worker se nesmí přihlašovat uživatelskou session.

## Soubory

- `scripts/freio-prospecting/discover.py` — discovery CLI a Claude adapter.
- `scripts/freio-prospecting/submit.py` — validate-only/explicit-send CLI.
- `scripts/freio-prospecting/freio_prospecting/` — validace, fetch, podpis a workflow.
- `scripts/freio-prospecting/schemas/` — schema nedůvěryhodného research výstupu a ověřené dávky.
- `scripts/freio-prospecting/prompts/discover-v1.txt` — research-only prompt bez oprávnění k mutacím.
- `scripts/systemd/freio-prospect-{discovery,submit}.{service,timer}` — disabled-by-default šablony.

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
sudo useradd --system --gid freio-prospecting --home-dir /var/lib/freio-prospecting/claude-home --shell /usr/sbin/nologin freio-discovery
sudo useradd --system --gid freio-prospecting --home-dir /nonexistent --shell /usr/sbin/nologin freio-submit
sudo install -d -o root -g freio-prospecting -m 0750 /var/lib/freio-prospecting /var/lib/freio-prospecting/quarantine
sudo install -d -o freio-discovery -g freio-prospecting -m 0770 /var/lib/freio-prospecting/ready
sudo install -d -o freio-discovery -g freio-prospecting -m 0700 /var/lib/freio-prospecting/claude-home /var/lib/freio-prospecting/quarantine/untrusted
sudo install -d -o freio-submit -g freio-prospecting -m 0700 /var/lib/freio-prospecting/processing /var/lib/freio-prospecting/processed /var/lib/freio-prospecting/quarantine/claimed
sudo install -d -o root -g root -m 0700 /etc/freio-prospecting /etc/freio-prospecting/enabled
sudo install -o root -g root -m 0644 scripts/systemd/freio-prospect-discovery.service scripts/systemd/freio-prospect-discovery.timer scripts/systemd/freio-prospect-submit.service scripts/systemd/freio-prospect-submit.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Do tohoto okamžiku se nevytváří `/etc/freio-prospecting/enabled/discovery`, `/etc/freio-prospecting/enabled/submit`, nevolá se `systemctl enable` a neukládají se credentials.

Pro budoucí Claude discovery se uloží samostatný token do `/etc/freio-prospecting/claude-token` s režimem `0600`. Pro submit se použije jiný náhodný klíč v `/etc/freio-prospecting/submit-hmac` s režimem `0600`. Tyto soubory nikdy nepatří do Gitu. `submit.env` obsahuje pouze necitlivou přesnou endpoint URL:

```ini
FREIO_PROSPECTING_ENDPOINT=https://outreach.freio.cz/api/internal/growth-partners/prospect-intake
```

## Budoucí řízené zapnutí

Po nasazení a otestování HMAC receiveru se každá vrstva zapíná zvlášť:

1. Vytvořit pouze marker `enabled/discovery`, ručně spustit discovery service a zkontrolovat `ready`/`quarantine`.
2. Provést `submit.py --validate-only` a ručně zkontrolovat první dávku v dashboardu bez změny outreach automation.
3. Vytvořit marker `enabled/submit`, ručně spustit submit service a ověřit receipt v `processed`.
4. Teprve po těchto acceptance checks lze samostatně `enable --now` příslušný timer.
5. Master switch pro skutečné outreach odesílání zůstává mimo tento worker a musí zůstat vypnutý do samostatného schválení.

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

Podpis je `HMAC-SHA256(secret, canonical_request)` a posílá se jako `X-Freio-Prospecting-Signature: v1=<hex>`. Přesné další hlavičky jsou `X-Freio-Prospecting-Version`, `X-Freio-Prospecting-Timestamp`, `X-Freio-Prospecting-Nonce` a `X-Freio-Prospecting-Content-SHA256`; `Idempotency-Key` je receipt ID. Receiver musí podpis ověřit před parsováním JSON, odmítnout clock skew nad pět minut, atomicky rezervovat nonce a nikdy logovat podpis, body ani e-mail.

Request body je canonical UTF-8 JSON bez trailing newline a s přesnými top-level poli:

```text
{schemaVersion:"1", receipt:{algorithm,researchManifestSha256,intakePayloadSha256,evidenceSha256,signedItemsSha256,receiptId}, items:[...]}
```

Každý item má `idempotencyKey`, `prospect`, volitelný `emailCandidate` a povinný `fetchReceipt`. Fetch receipt má přesně `requestedUrl`, `finalUrl`, `fetchedAt`, `observedAt`, `status`, `redirectCount`, `mediaType`, `charset`, `byteLength`, `bodySha256`, `matchedEmailSha256`, `matchMethod`, `fetcherVersion`, `networkPolicyVersion` a `evidenceSha256`. Úplný strojový kontrakt je v `scripts/freio-prospecting/schemas/intake-batch.schema.json`.

- `intakePayloadSha256` je hash canonical `{items}` po odstranění `fetchReceipt` z každého itemu.
- `evidenceSha256` je hash canonical pole všech `fetchReceipt` ve stejném pořadí.
- `signedItemsSha256` je hash canonical obohaceného `items`; `receiptId` je `sha256:<signedItemsSha256>`.
- Každé `fetchReceipt.evidenceSha256` je hash stejného objektu bez samotného pole `evidenceSha256`.
- Stejný `receiptId` a stejné body musí receiver idempotentně vrátit jako HTTP 200/replayed. Stejný klíč s jiným body je HTTP 409.
