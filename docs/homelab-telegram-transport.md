# Bezpečný Telegram transport Homelabu

Tato vrstva sjednocuje provozní Telegram upozornění bez zpřístupnění bot tokenu
nebo chat ID běžným jobům. Stav v tomto repozitáři je záměrně **default-off**:
instalace sama nevytvoří aktivační marker, nespustí socket, nespustí
Alertmanager a neodešle testovací zprávu.

## Proč je nutná rotace

Historický token v `/srv/frem/telegram-token` byl dříve dostupný s příliš
širokými právy a jeho hodnota se objevila mimo bezpečnou credential hranici.
Nelze jej považovat za důvěryhodný, ani když má soubor nyní mód `0600`.
Je kompromitovaný a jeho použití je zakázané pro Freio, Alertmanager i nový
Homelab transport. Nesmí se kopírovat do nové cesty ani použít pro canary.

Soubor je však nyní stále aktivním credentialem odděleného izolovaného Frem
control bota. Pouze tento existující consumer jej smí dočasně používat do své
samostatné migrace nebo ukončení. Tato omezená výjimka z tokenu znovu nedělá
důvěryhodný credential a nesmí se rozšířit na žádný další proces. Freio cutover
tím není blokovaný: Freio, Alertmanager i nový transport používají nový
kanonický pár v `/etc/homelab-telegram/` a starou cestu mají nepřístupnou.

Také historický Alertmanager config obsahoval konkrétní chat ID v souboru
sledovaném gitem. Nový config používá `bot_token_file` i `chat_id_file`; žádná
hodnota proto není v gitu, argv ani běžném prostředí procesu.

## Architektura

- `/etc/homelab-telegram/telegram-token` a `telegram-chat-id` jsou jediný
  kanonický pár credential souborů, oba `0600 root:root` v adresáři
  `0700 root:root`.
- Legacy job předá maximálně 8 KiB UTF-8 textu přes stdin do `notify.sh`.
- Neprivilegovaný klient pošle text přes lokální Unix socket. Socket smí otevřít
  jen root nebo člen skupiny `telegram-notify`.
- Socket node má mód `0660 root:telegram-notify`; veřejně průchozí runtime
  adresář neuděluje možnost spojení. Socket aktivuje krátký sandboxovaný proces.
  Pouze ten dostane credential přes
  systemd `LoadCredential=` a provede přímé TLS spojení na `api.telegram.org`.
- Freio dispatcher si zachovává vlastní idempotentní odesílací logiku, ale přes
  `LoadCredential=` čte stejný kanonický pár. Machine secret zůstává samostatný.
- Alertmanager 0.33.1 čte oba údaje přes nativní `*_file` volby z read-only
  bind mountů. Config neobsahuje jejich hodnoty.

Transport nepoužívá proxy z prostředí, nesleduje redirecty a do journalu nikdy
nezapisuje zprávu, token, chat ID ani Telegram response body. Link preview je
vypnutý a core dumpy transportu, Freio dispatcher i Alertmanager mají zakázané.

## Instalace bez aktivace

Spouštět z `/srv/homelab`:

```bash
sudo groupadd --system --force telegram-notify
sudo install -D -m 0755 scripts/homelab_telegram_notify/client.py \
  /usr/local/libexec/homelab-telegram-notify-client
sudo install -D -m 0755 scripts/homelab_telegram_notify/transport.py \
  /usr/local/libexec/homelab-telegram-notify-transport
sudo install -D -m 0755 self-healing/notify.sh \
  /srv/homelab/self-healing/notify.sh
sudo install -D -m 0644 scripts/systemd/homelab-telegram-notify.socket \
  /etc/systemd/system/homelab-telegram-notify.socket
sudo install -D -m 0644 scripts/systemd/homelab-telegram-notify@.service \
  /etc/systemd/system/homelab-telegram-notify@.service
```

Nainstalovat také změněné jednotky callerů. `SupplementaryGroups=` umožní
uživateli `anakin` zapisovat pouze do notifikačního socketu, ne číst credential:

```bash
sudo install -m 0644 scripts/systemd/self-healing.service \
  scripts/systemd/daily-health-review.service \
  scripts/systemd/self-improve.service \
  scripts/systemd/notify-failure@.service \
  scripts/systemd/backup-notify-failure.service \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/homelab-telegram-notify.socket \
  /etc/systemd/system/homelab-telegram-notify@.service
```

V této fázi nevytvářet `/etc/homelab-telegram/enabled` a socket nezapínat.

## Credential po rotaci

Teprve po vydání nového tokenu vytvořit kanonické soubory. Hodnoty zadat přes
`sudoedit`, nikdy je nepřidávat do příkazu, shell history nebo `.env`:

```bash
sudo install -d -o root -g root -m 0700 /etc/homelab-telegram
sudo install -o root -g root -m 0600 /dev/null \
  /etc/homelab-telegram/telegram-token
sudo install -o root -g root -m 0600 /dev/null \
  /etc/homelab-telegram/telegram-chat-id
sudoedit /etc/homelab-telegram/telegram-token
sudoedit /etc/homelab-telegram/telegram-chat-id
sudo stat -c '%a %U:%G %n' /etc/homelab-telegram \
  /etc/homelab-telegram/telegram-token \
  /etc/homelab-telegram/telegram-chat-id
```

Očekávání: adresář `700 root:root`, oba soubory `600 root:root`. Soubory nesmí
být symlink. Token i chat ID smějí mít jeden koncový newline.

## Alertmanager migrace

Nový `compose/observability/alertmanager/alertmanager.yml` neobsahuje hodnoty.
Je kompatibilní s pinem `prom/alertmanager:v0.33.1`, který podporuje
`chat_id_file`. Compose spouští Alertmanager bez Linux capabilities, read-only
a s odděleným datovým volume. UID 0 uvnitř neprivilegovaného kontejneru je
nutné jen pro čtení host credential `0600 root:root`; kontejner nemá Docker
socket ani jiné host mounty. Služba je navíc v profilu `telegram`, takže běžné
`docker compose up -d` ji před rotací samo nespustí. Credential bindy mají
`create_host_path: false`; chybějící soubor se nikdy nezmění na root-owned
adresář vytvořený Dockerem.

Po instalaci nových credential nejprve pouze vykreslit a zkontrolovat config:

```bash
cd /srv/homelab/compose/observability
sudo docker compose -p observability config --quiet
sudo docker run --rm \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$PWD/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
  -v /etc/homelab-telegram/telegram-token:/run/credentials/telegram-token:ro \
  -v /etc/homelab-telegram/telegram-chat-id:/run/credentials/telegram-chat-id:ro \
  --user 0:0 --entrypoint amtool prom/alertmanager:v0.33.1 \
  check-config /etc/alertmanager/alertmanager.yml
```

Teprve v řízeném aktivačním okně nahradit historický `obs-alertmanager`:

```bash
sudo docker compose -p observability up -d --no-deps obs-alertmanager
sudo docker exec obs-alertmanager amtool check-config \
  /etc/alertmanager/alertmanager.yml
```

Dočasně sledovat jen obecný stav kontejneru a logy. Do diagnostických příkazů
nikdy nevkládat token ani chat ID.

## Řízená aktivace socketu

Po rotaci, instalaci a statické validaci vytvořit marker a zapnout socket:

```bash
sudo install -o root -g root -m 000 /dev/null \
  /etc/homelab-telegram/enabled
sudo systemctl enable --now homelab-telegram-notify.socket
sudo systemctl status homelab-telegram-notify.socket --no-pager
```

Canary zprávu lze předat pouze stdin, například z root-only terminálu. Nikdy
nepoužívat starý `notify.sh "text"` tvar ani `curl` s tokenem v URL argumentu.
Po úspěšném canary restartovat tři jednotky běžící jako `anakin`, aby převzaly
novou supplementary group konfiguraci.

Freio dispatcher aktivovat samostatně podle
`docs/freio-telegram-handoff.md`; jeho marker a machine secret nejsou sdílené.

## Rollback a odstranění historických zdrojů

Bezpečný rollback transportu nevyžaduje smazání dat:

```bash
sudo systemctl disable --now homelab-telegram-notify.socket
sudo mv /etc/homelab-telegram/enabled \
  /etc/homelab-telegram/enabled.disabled
```

`/srv/frem/telegram-token` dnes ještě není revoked a nesmí se zatím unlinknout.
Jeho retirement má pevné pořadí:

1. Oddělený Frem control bot migrovat na vlastní nový credential, nebo jej
   prokazatelně ukončit.
2. Teprve potom starý token revoke přes BotFather.
3. Bez čtení hodnoty ověřit, že cestu nepoužívá žádný consumer, container mount
   ani otevřený file descriptor. Historické textové zmínky a sandboxové
   `InaccessiblePaths=` nejsou consumer, ale žádný spustitelný config nesmí
   starý soubor načítat.
4. Až po těchto třech gatech odstranit přesně tento soubor pomocí
   `sudo unlink -- /srv/frem/telegram-token`.

Tracked hodnotu chat ID opravit také v repozitáři `/srv/frem/repo`. Samotné
odstranění z aktuálního commitu nemaže historii; chat ID považovat za
zveřejněné a token za kompromitovaný. Přepis git historie je samostatná
koordinovaná operace a není podmínkou bezpečného Freio cutoveru.
