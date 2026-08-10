# Freio B2B inbound worker

Tento worker zpracovává už přijaté B2B odpovědi. Obsah e-mailu klasifikuje pouze lokálními deterministickými pravidly. Nepoužívá Claude, jiný externí model ani síťovou službu pro klasifikaci, takže předměty a těla zpráv neopouštějí infrastrukturu Freio.

Worker sám nevytváří volný text, nemění ceník, neschvaluje slevu ani neurčuje příjemce. Server Freio znovu ověří kanonickou zprávu v LaunchMail, oprávnění přesného vlákna, odesílatele, limity a confidence. Teprve potom buď pošle auditovanou šablonu, zaznamená odmítnutí, nebo vytvoří owner handoff.

Implementace i timer jsou defaultně vypnuté. Služba vyžaduje marker `/etc/freio-b2b-agent/enabled/classifier`; instalační postup ho nevytváří.

## Soubory a hranice

- `scripts/freio_b2b_agent/worker.py` je jediný CLI entry point.
- `scripts/freio_b2b_agent/local_classifier.py` obsahuje malý auditovatelný Czech/English ruleset.
- `scripts/freio_b2b_agent/contract.py` validuje exact-key claim a classification payload.
- `scripts/freio_b2b_agent/http_client.py` používá pevný HTTPS origin bez proxy a redirectů.
- `scripts/freio_b2b_agent/state.py` drží privátní durable spool a reconciliation circuit.
- `scripts/systemd/freio-b2b-agent.{service,timer}` jsou marker-gated hardened jednotky.

Lokální klasifikátor automaticky pouští jen vysokosignální standardní scénáře: zájem, požadavek na standardní cenu, čtyři podporované FAQ, explicitní odmítnutí, unsubscribe a automatickou odpověď. Počet licencí a předmětů vytěžuje pouze z explicitních formulací. Schůzka, nasazení, sleva, individuální podmínky, smlouva, privacy, bezpečnost, procurement, stížnost, prompt injection a každý neznámý případ se předá vlastníkovi. Summary je pevný popis kategorie a nikdy nekopíruje zákaznický obsah.

## API kontrakt

Oba requesty používají `Authorization: Bearer <dedicated-secret>`, JSON, standardní TLS, fixed host `https://outreach.freio.cz`, žádné environment proxy a žádné redirecty. Secret se čte jen ze systemd `LoadCredential`.

Claim:

```text
POST /api/internal/b2b-agent/tasks/claim
{}
```

Odpověď je přesně `{"task":null}` nebo bounded task s UUID, `leadType`, `subject`, `content` a `autonomousTurns`. Complete posílá pouze `taskId`, `claimId` a bounded classification; nikdy neposílá draft ani volný text odpovědi:

```text
POST /api/internal/b2b-agent/tasks/complete
```

Jediné potvrzení complete je HTTP 200 s přesným `{"success":true}`. Neznámé pole, duplicate JSON key, NaN/Infinity, nekanonické UUID, redirect, nečekaný content type, timeout nebo příliš velké body nejsou úspěch.

## Durable spool

Před complete POST worker atomicky a s `fsync` uloží exact payload do `/var/lib/freio-b2b-agent/completion.json` ve fázi `prepared`, potom ho přepne na `inflight`.

- Pád ve fázi `prepared`: další run zopakuje pouze stejný complete a nic znovu neclaimuje.
- Nejednoznačný výsledek ve fázi `inflight`: vznikne hash-only `uncertain.json`, classification payload se smaže a circuit se otevře.
- Přesný úspěch: vznikne hash-only `last-success.json` a citlivý spool se odstraní.
- Chyba klasifikace: complete se neposílá a serverová lease může bezpečně vypršet.

Journal nikdy neloguje UUID, předmět, obsah, summary, response body ani credential. Nejasný complete se automaticky neopakuje.

## Příprava a aktivace

```bash
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin freio-b2b-agent
sudo install -d -o root -g root -m 0700 /etc/freio-b2b-agent /etc/freio-b2b-agent/enabled
sudo install -d -o freio-b2b-agent -g freio-b2b-agent -m 0700 /var/lib/freio-b2b-agent
sudo install -o root -g root -m 0600 /dev/stdin /etc/freio-b2b-agent/api-bearer
sudo install -o root -g root -m 0644 scripts/systemd/freio-b2b-agent.service scripts/systemd/freio-b2b-agent.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

`api-bearer` musí být dedikovaný jen classifier endpointům a odlišný od Telegramu, discovery, Supabase a LaunchMail. Před schváleným canary musí platit:

```bash
test ! -e /etc/freio-b2b-agent/enabled/classifier
systemctl is-enabled freio-b2b-agent.timer
```

Marker a timer se aktivují až po nasazení serverového kontraktu, DB lease recovery, owner monitoringu a interním canary přes owner-controlled mailbox. Veřejně nalezený kontakt ani cold outreach nejsou platným canary.

## Ověření

```bash
python3 -m unittest discover -s tests/freio_b2b_agent -v
python3 -m compileall -q scripts/freio_b2b_agent tests/freio_b2b_agent
ruff check scripts/freio_b2b_agent tests/freio_b2b_agent
mypy --strict scripts/freio_b2b_agent
systemd-analyze verify scripts/systemd/freio-b2b-agent.service scripts/systemd/freio-b2b-agent.timer
```

Testy nepoužívají produkční síť ani credential.

## Ruční reconciliation

Pokud vznikne `uncertain.json`, zastavit timer a v DB/API audit ledgeru ověřit `taskId`, `claimId` a `completionSha256`. Záznam neobsahuje e-mailový obsah a nemá se kopírovat mimo interní incident.

Pokud server complete prokazatelně zapsal:

```bash
sudo -u freio-b2b-agent /usr/bin/python3 /srv/homelab/scripts/freio_b2b_agent/worker.py --spool /var/lib/freio-b2b-agent --reconcile-completed
```

Pokud ho prokazatelně nezapsal a claim už server bezpečně uvolnil:

```bash
sudo -u freio-b2b-agent /usr/bin/python3 /srv/homelab/scripts/freio_b2b_agent/worker.py --spool /var/lib/freio-b2b-agent --reconcile-not-completed
```

Reconciliation pouze zavře lokální circuit. Nic neposílá a neaktivuje timer.
