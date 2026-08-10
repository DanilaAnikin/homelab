# Freio Telegram handoff dispatcher

Tento dispatcher doručuje vlastníkovi nejvýše jednu obecnou Telegram notifikaci
za jeden běh. Claim, leasing, deduplikaci, retry plán a limit osmi pokusů vlastní
Freio databáze. Lokální stav chrání přesně-jednou rozhodnutí v okamžiku, kdy už
mohl Telegram zprávu přijmout, ale výsledek finalize není potvrzený.

## Bezpečnostní stav a podmínky aktivace

Dispatcher je po instalaci **vypnutý**. Služba se nespustí bez explicitního
markeru `/etc/freio-telegram-handoff/enabled` a timer se nesmí zapnout v rámci
instalace.

Před vytvořením markeru musí být splněno vše:

1. Rotovat produkční Telegram bot token. Audit nalezl starý tokenový soubor
   `/srv/frem/telegram-token` s příliš širokým lokálním oprávněním; tento token
   se nesmí znovu použít. Nový společný pár musí být připraven podle
   `docs/homelab-telegram-transport.md` v `/etc/homelab-telegram/`.
2. Vytvořit nový dedikovaný Freio machine secret a stejnou hodnotu bezpečně
   nastavit na Freio API i do credential souboru služby.
3. Produkční API musí podporovat idempotentní claim/finalize kontrakt popsaný
   níže a databázový limit nejvýše osmi pokusů.
4. Ověřit, že `outreach.freio.cz` je dostupné pouze z Tailnetu.

Token, chat ID ani machine secret nesmí být v argumentech procesu, běžných
proměnných prostředí nebo logu. Jednotka je načítá pouze přes systemd
`LoadCredential=`. Sandbox navíc procesu výslovně skrývá starý tokenový soubor
i obecný adresář Homelab secrets.

## Instalace bez aktivace

Spouštět z kořene repozitáře na Homelabu:

```bash
sudo install -D -m 0755 scripts/freio_telegram_handoff/dispatcher.py \
  /usr/local/libexec/freio-telegram-handoff
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff.service \
  /etc/systemd/system/freio-telegram-handoff.service
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff.timer \
  /etc/systemd/system/freio-telegram-handoff.timer
sudo install -d -o root -g root -m 0700 /etc/freio-telegram-handoff
sudo systemctl daemon-reload
```

Záměrně zde není `enable`, `start` ani aktivační marker.

Telegram token a chat ID se zde neduplikují. Služba je čte přes
`LoadCredential=` z kanonických root-only souborů
`/etc/homelab-telegram/telegram-token` a `telegram-chat-id`. Jejich vytvoření,
rotace a kontrola oprávnění jsou v `docs/homelab-telegram-transport.md`.

Zde vytvořit pouze dedikovaný Freio machine secret. Soubor nesmí být symlink a
smí obsahovat jen hodnotu a volitelný poslední newline:

```bash
sudo install -o root -g root -m 0600 /dev/null \
  /etc/freio-telegram-handoff/freio-machine-secret
sudoedit /etc/freio-telegram-handoff/freio-machine-secret
sudo systemd-analyze verify \
  /etc/systemd/system/freio-telegram-handoff.service \
  /etc/systemd/system/freio-telegram-handoff.timer
```

Ověřit oprávnění bez čtení obsahu:

```bash
sudo stat -c '%a %U:%G %n' /etc/homelab-telegram \
  /etc/homelab-telegram/telegram-token \
  /etc/homelab-telegram/telegram-chat-id \
  /etc/freio-telegram-handoff \
  /etc/freio-telegram-handoff/freio-machine-secret
```

Očekávání: oba adresáře `700 root:root`, credential soubory `600 root:root`.

## Přesný síťový kontrakt

Claim je `POST` bez body na
`https://outreach.freio.cz/api/internal/b2b-agent/notifications/claim` s
dedikovaným Bearer machine secretem. Úspěch je HTTP 200 `application/json` a
přesně jeden top-level klíč:

```json
{"notification":null}
```

nebo objekt s přesnými klíči `id`, `claimId`, `eventId`, `kind`,
`actionSummary`, `deepLink`.
Všechny tři identifikátory musí být kanonické UUID. Historicky pojmenované
`eventId` v tomto kontraktu vždy obsahuje UUID konverzace. Dispatcher povolí
jen tento deep link na celé vlákno, bez dalších parametrů, jiné cesty nebo
jiného hostu:

- `https://outreach.freio.cz/?section=conversations&conversation=<UUID>`

`actionSummary` je přesný PII-free objekt sestavený Freio API pouze ze
strukturovaných ledgerů:

```json
{
  "intent": "pricing",
  "offerCount": 2,
  "latestOffer": {"totalCzk": 240000, "currency": "CZK"}
}
```

`intent` je `null` nebo jeden z allowlistovaných enumů rozhodovacího ledgeru.
`offerCount` je celé číslo od `0` do `10000`. `latestOffer` je `null` právě
tehdy, když je počet nabídek nulový; jinak obsahuje pouze nezáporné
`totalCzk` do `2147483647` a literál `CZK`. Jakýkoli další klíč, volný text,
raw e-mail, jméno, adresa nebo LLM shrnutí poruší kontrakt a dispatcher claim
failne zavřeně.

Finalize je idempotentní `POST` JSON na
`https://outreach.freio.cz/api/internal/b2b-agent/notifications/finalize`.
Obsahuje `notificationId`, `claimId`, `outcome` a jen relevantní volitelné pole.
Úspěch je HTTP 200 `application/json` s `{"success":true}`. Opakování stejného
finalize po ztracené odpovědi musí vrátit stejný úspěch.

Telegram zpráva neobsahuje PII. Dispatcher mapuje `kind` na krátkou akci a
`intent` na deterministický popis potřeby klienta. Dále zobrazí jen počet
nabídek, případnou poslední cenu v CZK a jediný výše allowlistovaný Tailnet
deep link na celé vlákno. Text e-mailu ani modelové shrnutí nepřijímá.
Redirecty a proxy z prostředí jsou zakázané.

## Řízená aktivace

Nejprve vytvořit marker bez obsahu a spustit jediný canary běh:

```bash
sudo install -o root -g root -m 000 /dev/null \
  /etc/freio-telegram-handoff/enabled
sudo systemctl start freio-telegram-handoff.service
sudo systemctl status freio-telegram-handoff.service --no-pager
sudo journalctl -u freio-telegram-handoff.service -n 50 --no-pager
```

Canary zpracuje nanejvýš jeden claim. Log musí obsahovat pouze obecné JSON
události, žádný token, chat ID, UUID, URL, e-mail ani HTTP body. Teprve po
ověření canary zapnout timer:

```bash
sudo systemctl enable --now freio-telegram-handoff.timer
systemctl list-timers freio-telegram-handoff.timer --no-pager
```

## Chování chyb

- Timeout nebo nejednoznačný transport po Telegram requestu se finalizuje jako
  `uncertain`; stejná zpráva se už znovu neposílá.
- HTTP 408, 425, 429 a 5xx se finalizují jako `retry`. `retry_after` se
  respektuje v rozsahu 30 až 86400 sekund.
- HTTP 400, 401, 403 a jiné trvalé 4xx se finalizují jako `dead` a otevřou
  circuit breaker.
- TLS nebo porušení response/deep-link kontraktu failne zavřeně a otevře
  circuit breaker.
- Ještě před vstupem do Telegram requestu se atomicky uloží konzervativní
  `uncertain` finalize. Pád procesu v libovolném okamžiku odesílání proto po
  restartu nikdy nevyvolá Telegram resend.
- Před každým novým claimem se nejprve idempotentně dokončí soubor
  `/var/lib/freio-telegram-handoff/pending-finalize-v1.json`.

Soubor `pending-finalize-v1.json` nikdy ručně nemažte: po přijetí Telegramem je
jedinou ochranou před opětovným odesláním.

## Vypnutí a zotavení circuit breakeru

Bezpečné vypnutí je vratné:

```bash
sudo systemctl disable --now freio-telegram-handoff.timer
sudo mv /etc/freio-telegram-handoff/enabled \
  /etc/freio-telegram-handoff/enabled.disabled
```

Při otevřeném circuit breakeru nejprve nechat timer vypnutý, opravit příčinu a
případně rotovat credential. Po ověření opravy přesunout
`/var/lib/freio-telegram-handoff/circuit-open-v1.json` do root-only archivu mimo
StateDirectory; soubor nemazat. Potom obnovit aktivační marker a spustit službu
jednou. Existující pending finalize dispatcher přehraje před jakýmkoli novým
claimem a bez Telegram resend; při neúspěchu circuit znovu otevře. Po potvrzeném
finalize spustit ještě jeden jednotlivý canary a až potom znovu zapnout timer.

Nikdy neladit pomocí `curl` příkazu s tokenem nebo secretem v argv. Stav
kontrolovat přes obecné journal události, stav systemd a dashboard/API metriky.
