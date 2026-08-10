# Freio B2B discovery worker V1

Tato větev hledá veřejné potenciální B2B zákazníky Freio, primárně české
doučovací, jazykové a komerčně vzdělávací právnické osoby. Je pouze discovery +
intake:
nikdy neposílá e-mail, nevyplňuje formulář, nedomlouvá cenu, nevytváří souhlas,
autorizaci kontaktu ani outreach zprávu. Nový lead na straně Freio zůstává
`stage=new`, `outreach_status=not_enrolled`.

Repozitář obsahuje jen **default-off šablony**. V tomto kroku nebyl vytvořen
žádný uživatel, credential, marker ani systemd symlink a nic nebylo spuštěno,
nasazeno nebo aktivováno.

## Dvě oddělené bezpečnostní identity

1. `freio-b2b-discover` má pouze samostatný dlouhodobý Anthropic API key. Claude
   je připnutý na `claude-sonnet-5`, smí použít jen `WebSearch,WebFetch`, běží
   v neinteraktivním režimu `dontAsk` s tvrdým stropem 1 USD na jeden běh,
   vrací nedůvěryhodný JSON a nemá intake HMAC.
2. `freio-b2b-intake` nemá Claude credential. Kandidáta znovu načte vlastním
   deterministickým fetcherem, přesně ověří publikovaný e-mail, vytvoří receipt,
   uloží canonical request před POSTem a podepíše jej samostatným HMAC.

Obě identity sdílejí pouze `ready` adresář přes skupinu
`freio-b2b-discovery`. Privátní discovery a submit spooly mají režim `0700`.
Hotový ready manifest je před atomickým rename nastaven přesně na `0640`:
discovery identita jej vlastní a submit identita jej může pouze číst. Submit
service dostává celý spool jako jedinou writable mount hranici, protože přesun
`ready -> processing -> processed|quarantine/claimed` musí zůstat atomický;
Unix ownership a režimy nadále blokují submit identitě discovery-only privátní
podadresáře.
Inbound classifier `scripts/freio_b2b_agent`, influencer prospecting i Telegram
dispatcher mají jiné Unix identity, spooly a credentials.

Worker používá auditovaný SSRF/fetch a canonical JSON základ z
`scripts/freio-prospecting`. B2B model, prompt, HMAC namespace, endpoint, spool i
identity index jsou samostatné.

## Co worker přijme

- nejvýše 10 kandidátů z jednoho Claude běhu;
- pouze `leadType=tutoring|company`, nikdy `school` ani RED-IZO;
- pouze právnickou osobu s přesným veřejným právním názvem, povinnou allowlisted
  právní formou a povinným IČO, které projde lokálním českým checksumem;
- veřejnou `legalEntitySourceUrl` na oficiálním webu, kde trusted fetcher ve
  viditelném textu přesně ověří právní název, formu a IČO;
- nikdy OSVČ, fyzickou osobu, freelancera, osobní jméno, person mailbox ani
  person `name`/`role` pole;
- canonical veřejný HTTPS origin s trailing `/` jako přesnou identitu firmy;
- evidence URL na stejném oficiálním hostname;
- pouze allowlisted obecný inbox (`info@`, `kontakt@`, `obchod@`, `sales@`,
  `contact@`, `office@`, `podpora@` a další explicitní role aliasy), který je
  přesně přítomný ve viditelném textu nebo `mailto:` na evidence stránce;
- bounded volitelné organizační údaje;
- exact-key JSON bez neznámých polí.

Modelový WebFetch není důkaz. Discovery udělá preview fetch kontaktu i právnické
osoby a submit identita udělá oba trusted refetch znovu těsně před sestavením
requestu. Pokud jde o jednu stránku, stejné tělo lze ověřit pro oba účely. Každý
hostname i redirect se znovu DNS-resolvuje; jediná neveřejná A/AAAA odpověď celý
fetch odmítne. Spojení je připnuté na validovanou IP při zachování TLS/SNI.
Povolen je jen HTTPS/443, nejvýše 3 redirecty, celkový 10s deadline, HTTP 200,
`text/html|text/plain`, nekomprimované tělo do 1 MiB a jednoznačné kritické
hlavičky. Raw tělo stránky se neukládá ani neposílá. Nevalidní modelový JSON
může obsahovat neočekávané osobní údaje, proto se ani v quarantine neukládá:
zůstane pouze SHA-256, byte length a PII-free reason code.

## Durable spool, idempotence a retence

Každý kandidát je samostatný canonical content-addressed manifest. Submit
atomicky provede `ready -> processing`, následně uloží exact canonical body jako
`*.request.json` **před prvním POSTem**. Retry používá stejné body a receipt ID,
ale nový timestamp, nonce a HMAC, protože receiver má pětiminutové replay okno.

PII-free identity index obsahuje jen domain-separated keyed HMAC webu a e-mailu,
receipt ID, hash research manifestu a stav `pending|accepted|uncertain`.
Transportní HMAC lze rotovat samostatně. Identity HMAC se nesmí běžně rotovat:
z uložených digestů nelze původní identity zrekonstruovat; změna vyžaduje
zastavení submitu a DB-backed rebuild indexu.

- raw ready/deferred/quarantine/request data: nejvýše 72 hodin;
- hash-only processed receipts: 30 dní;
- transient retry: 5 min, 15 min, 1 h, 3 h, max. 5 pokusů / 24 h;
- HTTP 429 se odloží nejméně do 00:05 UTC následujícího dne;
- jeden běh submitu zpracuje nejvýše 5 kandidátů za nejvýše 150 sekund.

DNS/network/timeout a HTTP `408`, `425`, `429`, `5xx` jsou transient. Nevalidní
2xx, příliš velká/protokolově chybná odpověď a netransient globální HTTP chyba
(`400/401/403/404/405/413/415` apod.) otevřou hash-only global circuit. Circuit
se nikdy sám nezavře. `409` nebo vyčerpaný retry po možném POSTu vytvoří
`uncertain` bundle pro operator reconciliation; worker jej automaticky znovu
nepošle.

## Soubory

- `scripts/freio-b2b-discovery/discover.py` — Claude/offline discovery CLI.
- `scripts/freio-b2b-discovery/submit.py` — validate, HMAC intake a recovery CLI.
- `scripts/freio-b2b-discovery/freio_b2b_discovery/` — strict model, spool,
  signing, discovery a submission.
- `scripts/freio-b2b-discovery/prompts/discover-v1.txt` — research-only prompt.
- `scripts/freio-b2b-discovery/schemas/candidate-research-v1.schema.json` —
  modelový schema hint; autoritativní validace je Python parser.
- `scripts/systemd/freio-b2b-discovery-*.{service,timer}` — default-off šablony.
- `tests/freio_b2b_discovery/` — offline testy se síťovými mocky.

## Lokální ověření bez API mutace

Následující příkazy nic neposílají do Freio API:

```bash
python3 -m unittest discover -s tests/freio_b2b_discovery -v
ruff check scripts/freio-b2b-discovery tests/freio_b2b_discovery
ruff format --check scripts/freio-b2b-discovery tests/freio_b2b_discovery
python3 -m compileall -q scripts/freio-b2b-discovery

python3 scripts/freio-b2b-discovery/discover.py \
  --spool /tmp/freio-b2b-discovery \
  --input-json /path/to/operator-reviewed-candidates.json

python3 scripts/freio-b2b-discovery/submit.py \
  --spool /tmp/freio-b2b-discovery \
  --endpoint https://outreach.freio.cz/api/internal/b2b-agent/prospect-intake \
  --validate-only
```

`--validate-only` nečte secret, nedělá refetch/POST a nemění spool.

## Preconditions před jakoukoli instalací

Neaktivovat, dokud současně neplatí:

1. Freio release obsahuje migraci `20260810193000_b2b_discovery_intake.sql` a
   přesný HMAC handler `/api/internal/b2b-agent/prospect-intake`.
2. `middleware.ts` explicitně propouští pouze exact `POST` na tento path, HTTPS
   authority `outreach.freio.cz`, bez query. Bez toho vrací private host 404.
3. Produkční env Freio obsahuje nový a nikde nereusovaný
   `FREIO_B2B_DISCOVERY_HMAC_SECRET`.
4. `/usr/local/bin/claude` je samostatně nainstalován a připnut na přesně
   schválenou verzi `2.1.226`; verze i původ artefaktu jsou ověřené před canary.
   Worker navíc vždy předává přesný model `claude-sonnet-5`, rozpočtový limit
   `1.00` USD a `--permission-mode dontAsk`.
5. Receiver je canary ověřen s vypnutými DB gates a následně s jedním ručně
   kontrolovaným kandidátem.
6. Operator/compliance samostatně schválil `b2b_autonomy_settings` a zapnul jen
   `discovery_enabled` + `intake_enabled`. Outbound gate zůstává oddělený.
7. Oba housekeeping běhy jsou funkční dříve než discovery/submit timery.

## Budoucí instalace bez zapnutí

Tyto příkazy jsou runbook, v tomto kroku se nespouštěly:

```bash
sudo groupadd --system freio-b2b-discovery
sudo useradd --system --gid freio-b2b-discovery --home-dir /nonexistent --shell /usr/sbin/nologin freio-b2b-discover
sudo useradd --system --gid freio-b2b-discovery --home-dir /nonexistent --shell /usr/sbin/nologin freio-b2b-intake

sudo install -d -o root -g freio-b2b-discovery -m 0750 /var/lib/freio-b2b-discovery
sudo install -d -o freio-b2b-discover -g freio-b2b-discovery -m 0770 /var/lib/freio-b2b-discovery/ready
sudo install -d -o freio-b2b-intake -g freio-b2b-discovery -m 0700 /var/lib/freio-b2b-discovery/processing /var/lib/freio-b2b-discovery/processed /var/lib/freio-b2b-discovery/state
sudo install -d -o root -g freio-b2b-discovery -m 0750 /var/lib/freio-b2b-discovery/deferred /var/lib/freio-b2b-discovery/quarantine
sudo install -d -o freio-b2b-discover -g freio-b2b-discovery -m 0700 /var/lib/freio-b2b-discovery/deferred/discovery /var/lib/freio-b2b-discovery/quarantine/discovery
sudo install -d -o freio-b2b-intake -g freio-b2b-discovery -m 0700 /var/lib/freio-b2b-discovery/quarantine/claimed

sudo install -d -o root -g root -m 0700 /etc/freio-b2b-discovery /etc/freio-b2b-discovery/enabled
sudo install -o root -g root -m 0644 scripts/systemd/freio-b2b-discovery-*.service scripts/systemd/freio-b2b-discovery-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

V tomto bodě se nevytváří žádný `enabled/*` marker a nevolá se
`systemctl enable` ani `systemctl start`.

## Credentials

Anthropic API key, transport HMAC a identity-index HMAC jsou tři různé
credentials. HMAC soubory obsahují literal ASCII výstup `openssl rand -hex 32`
(64 lowercase hex znaků, ne hex-decode). Soubory jsou root-only `0600` a do
workerů jdou pouze přes systemd `LoadCredential=`:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/freio-b2b-discovery/anthropic-api-key
sudoedit /etc/freio-b2b-discovery/anthropic-api-key
openssl rand -hex 32 | sudo tee /etc/freio-b2b-discovery/intake-hmac >/dev/null
openssl rand -hex 32 | sudo tee /etc/freio-b2b-discovery/identity-index-hmac >/dev/null
sudo chmod 0600 /etc/freio-b2b-discovery/anthropic-api-key \
  /etc/freio-b2b-discovery/intake-hmac \
  /etc/freio-b2b-discovery/identity-index-hmac
```

Do prvního souboru vložit pouze přesnou hodnotu dlouhodobého
`ANTHROPIC_API_KEY`, bez `export`, uvozovek či mezer. Discovery wrapper ji načte
z credential file a vloží jen do allowlisted prostředí child procesu Claude;
nepřebírá žádné ambientní proměnné. Prompt a schema posílá přes bounded `stdin`,
nikoli v argumentech procesu. Klíč nesmí být v env souboru, CLI argumentu ani
logu. Zdrojový credential zůstává root-only; systemd jej službě zpřístupní
read-only v `/run/credentials/.../anthropic-api-key`.

Před uložením ověřit, že `intake-hmac` není shodný s influencer, classifier,
Telegram, Supabase, session ani cron secretem. Stejných 64 znaků patří do
produkčního `FREIO_B2B_DISCOVERY_HMAC_SECRET`; hodnotu nikdy nelogovat.

`/etc/freio-b2b-discovery/submit.env` obsahuje jen necitlivý exact endpoint:

```ini
FREIO_B2B_DISCOVERY_ENDPOINT=https://outreach.freio.cz/api/internal/b2b-agent/prospect-intake
```

## Řízený canary a aktivace

1. Vytvořit pouze markery `enabled/discover-housekeeping` a
   `enabled/submit-housekeeping`; ručně spustit oba services a ověřit bounded
   výsledky. Teprve pak zapnout jejich timery.
2. Ověřit `/usr/local/bin/claude --version` proti pinu `2.1.226`. Vytvořit marker
   `enabled/discover`, ručně spustit discovery service a v `ready` ponechat
   právě jeden operator-reviewed kandidát. Kontrola musí potvrdit právnickou
   osobu, IČO checksum, oficiální legal-entity evidence a obecný inbox bez jména
   či role osoby.
3. Spustit `submit.py --validate-only`; stále bez credentialu a POSTu.
4. Po ověření Freio release/middleware/DB gate vytvořit marker `enabled/submit`
   a jednou ručně spustit submit service. Jeden ready manifest znamená jeden
   intake POST i přes hard limit 5.
5. V dashboardu ověřit lead `new/not_enrolled`, provenance a nulový počet nových
   contact authorization/outreach message záznamů.
6. Teprve po acceptance checks lze explicitně zapnout submit a discovery timer.

Existence timer souboru nic nezapíná. Ke každému běhu jsou potřeba současně
systemd timer a příslušný marker.

## HMAC receiver kontrakt

Canonical UTF-8 řetězec bez trailing newline:

```text
freio-b2b-discovery-v1
<unix timestamp>
<32 lowercase hex nonce>
POST
/api/internal/b2b-agent/prospect-intake
<sha256 exact canonical body bytes>
```

Hlavičky:

```text
X-Freio-B2B-Discovery-Version: 1
X-Freio-B2B-Discovery-Timestamp: <unix>
X-Freio-B2B-Discovery-Nonce: <32hex>
X-Freio-B2B-Discovery-Content-SHA256: <64hex>
X-Freio-B2B-Discovery-Signature: v1=<64hex>
Idempotency-Key: sha256:<itemsSha256>
Content-Type: application/json
```

HMAC-SHA256 používá 64 ASCII hex bytes credentialu. Request je canonical JSON do
64 KiB a obsahuje jeden item: organizační `lead`, `contact` pouze s generic
e-mailem, trusted contact + legal-entity `evidence`, receipt a idempotency key.
Receiver nepřijímá person `name`/`role`; povinné databázové jméno kontaktu
normalizuje na konstantu `Obecný firemní kontakt`. Nový retry zachová
body/receipt, ale použije nový nonce.

## Reconciliation a circuit

Nejasný receipt se rozhoduje až po kontrole Freio DB/receiver logu. Následující
CLI operace samy nic neposílají:

```text
submit.py --reconcile-accepted sha256:<64hex> --identity-secret-file <LoadCredential path>
submit.py --reconcile-not-accepted sha256:<64hex> --identity-secret-file <LoadCredential path>
submit.py --clear-global-error --identity-secret-file <LoadCredential path>
```

`accepted` smaže raw uncertain bundle a ponechá hash-only receipt/index.
`not-accepted` atomicky vrátí manifest do `ready` a odstraní jeho pending
identity tombstone. Circuit odstranit až po opravě credentialu, routy, deploye,
DB gate nebo response kontraktu; clear sám nic neposílá, další submit běh znovu
použije dochovaný canonical request nebo requeued manifest.

## Stop a rollback

1. Odstranit markery `enabled/discover` a `enabled/submit`.
2. Zastavit a disable oba hlavní timery; housekeeping ponechat do bezpečného
   vyřešení raw/uncertain dat.
3. Zkontrolovat `processing`, `quarantine/claimed` a global circuit. Nic z těchto
   adresářů nemažte před DB reconciliation.
4. V DB vypnout `discovery_enabled` a `intake_enabled`; outbound nastavení se
   tímto workerem nikdy nemění.
5. Teprve po expiraci/reconciliation lze odstranit spool a credentials.
