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
i obecný adresář Homelab secrets. Nenulový výsledek dispatcheru i samostatného
health-checku spadne přes `OnFailure=` do existujícího obecného
`notify-failure@.service`; žádný z těchto alertů nepřenáší původní e-mailový
obsah.

## Instalace bez aktivace

Spouštět z kořene repozitáře na Homelabu:

```bash
sudo install -D -m 0755 scripts/freio_telegram_handoff/dispatcher.py \
  /usr/local/libexec/freio-telegram-handoff
sudo install -D -m 0755 scripts/freio_telegram_handoff/health.py \
  /usr/local/libexec/freio-telegram-handoff-health
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff.service \
  /etc/systemd/system/freio-telegram-handoff.service
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff-canary.service \
  /etc/systemd/system/freio-telegram-handoff-canary.service
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff.timer \
  /etc/systemd/system/freio-telegram-handoff.timer
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff-health.service \
  /etc/systemd/system/freio-telegram-handoff-health.service
sudo install -D -m 0644 scripts/systemd/freio-telegram-handoff-health.timer \
  /etc/systemd/system/freio-telegram-handoff-health.timer
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
  /etc/systemd/system/freio-telegram-handoff-canary.service \
  /etc/systemd/system/freio-telegram-handoff.timer \
  /etc/systemd/system/freio-telegram-handoff-health.service \
  /etc/systemd/system/freio-telegram-handoff-health.timer
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

Claim je `POST` s canonical JSON tělem `{}` a hlavičkou
`Content-Type: application/json` na
`https://outreach.freio.cz/api/internal/b2b-agent/notifications/claim` s
dedikovaným Bearer machine secretem. Úspěch je HTTP 200 `application/json` a
přesně jeden top-level klíč:

```json
{"notification":null}
```

nebo objekt s přesnými klíči `id`, `claimId`, `eventId`, `kind`, `priority`,
`actionSummary`, `deepLink`. `priority` je přesně jeden z enumů `low`, `normal`,
`high`, `urgent`; žádný volný text se nepřenáší.
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

Předprodukční acceptance používá oddělený one-shot kontrakt. Dispatcher
spuštěný s jediným argumentem `--canary` pošle claim tělo
`{"canary":true}`. API smí odpovědět jen syntetickou notifikací s
`kind=system_canary`, `priority=normal`, prázdným strukturovaným kontextem,
`canary=true` a přesným odkazem
`https://outreach.freio.cz/?section=overview`. Canary UUID je současně
`id` i `eventId`; není to UUID zákazníka, konverzace ani eskalace. Finalize nese
stejný literál `canary=true`, takže nikdy nemůže změnit produkční outbox.
Canary proces navíc striktně vyžaduje `notification.canary=true`: odpověď
`notification=null`, běžná customer notifikace, malformed běžný payload nebo
pending finalize z běžného režimu otevře circuit bez Telegram send a bez
finalize. Běžný proces stejným způsobem odmítne canary claim/pending finalize.

Telegram zpráva neobsahuje PII. Dispatcher mapuje `kind` na krátkou akci,
`priority` na český allowlistovaný štítek a `intent` na deterministický popis
potřeby klienta. Dále zobrazí jen počet nabídek, případnou poslední cenu v CZK a
jediný výše allowlistovaný Tailnet deep link na celé vlákno. Text e-mailu ani
modelové shrnutí nepřijímá.
Redirecty a proxy z prostředí jsou zakázané.

## Privátní heartbeat a health-check

Každý dokončený běh dispatcheru atomicky přepíše
`/var/lib/freio-telegram-handoff/heartbeat-v1.json`. Soubor má mód `0600` uvnitř
`0700` systemd `StateDirectory` a obsahuje přesně jen verzi, UTC čas na sekundy a
jeden stav `idle`, `sent`, `retry` nebo `error`. Neobsahuje UUID, URL, e-mail,
chat ID, provider ID, credential ani text zprávy. Zápis probíhá pod stejným
zámkem jako běh workeru a je fsyncnutý před návratem služby.

Protože dispatcher používá `DynamicUser=true`, systemd mapuje tuto stabilní
cestu přes svůj symlink na privátní backing adresář
`/var/lib/private/freio-telegram-handoff`. Dispatcher povoluje pouze toto přesné
systemd mapování; jiný symlink odmítne. Root-only health služba čte přímo
privátní backing cestu.

Root-only health služba nečte Telegram ani Freio credential. Pokud existuje
bezpečný aktivační marker, ověří přes systemd, že
`freio-telegram-handoff.timer` je active, heartbeat má přesný privátní formát,
není více než 10 minut starý ani nepřiměřeně v budoucnosti a poslední stav je
`idle` nebo `sent`. Chybějící, poškozený či starý heartbeat a stavy `retry` nebo
`error` ukončí health službu nenulově. `OnFailure=` pak použije existující
`notify-failure@.service`; health log obsahuje pouze obecný event, stav a stáří
v sekundách.

Health služba potřebuje pouze `CAP_DAC_READ_SEARCH`, aby mohla přečíst privátní
StateDirectory dynamického uživatele. Nemá síťové address family, credentialy
ani zápis do systému. Health timer je stejně jako dispatcher timer po instalaci
vypnutý a nesmí se zapnout před úspěšným interním canary.

## One-shot acceptance a řízená aktivace

Canary nepoužívá `contact_inquiries`, konverzaci, owner escalation,
`b2b_notification_outbox`, e-mailový outbox ani outreach message. Nevytvářet
syntetickou inquiry a nikdy neresetovat existující `dead` notification. Canary
má vlastní immutable singleton ledger; všechny jiné výsledky než `sent` jsou
terminální a druhý send vyžaduje novou forward migraci a nové owner rozhodnutí.

Před autorizací musí být hlavní gate i oba timery vypnuté a produkční fronta
bez claimu. Následující SQL musí vrátit jediný řádek `f|0|0`:

```sql
SELECT settings.telegram_handoff_enabled,
       COUNT(*) FILTER (WHERE outbox.status IN ('pending', 'retry')),
       COUNT(*) FILTER (WHERE outbox.status = 'claimed')
FROM public.b2b_autonomy_settings AS settings
LEFT JOIN public.b2b_notification_outbox AS outbox ON TRUE
WHERE settings.id = 1
GROUP BY settings.telegram_handoff_enabled;
```

Postgres operátor pak jednou a nevratně zapíše explicitní autorizaci. Actor ani
reference nesmějí obsahovat jméno, e-mail nebo jiná customer data:

```sql
SELECT public.authorize_b2b_telegram_handoff_canary(
  'operator:freio-owner',
  'telegram-cutover-20260811',
  'AUTHORIZE_PII_FREE_TELEGRAM_HANDOFF_CANARY_V1'
);
```

Očekávání je přesně `{"success":true,"status":"authorized"}`. RPC není
dostupné roli `service_role`; opakování vrátí `canary_already_authorized` a
nikdy singleton znovu neodemkne.

Teprve potom vytvořit oddělený canary marker a spustit statickou službu. Hlavní
marker `/etc/freio-telegram-handoff/enabled` stále nesmí existovat:

```bash
test ! -e /etc/freio-telegram-handoff/enabled
test "$(systemctl is-enabled freio-telegram-handoff.timer)" = disabled
test "$(systemctl is-enabled freio-telegram-handoff-health.timer)" = disabled
sudo install -o root -g root -m 000 /dev/null \
  /etc/freio-telegram-handoff/canary-enabled
sudo systemctl start freio-telegram-handoff-canary.service
sudo systemctl status freio-telegram-handoff-canary.service --no-pager
sudo journalctl -u freio-telegram-handoff-canary.service -n 50 --no-pager
```

Canary zpracuje přesně jeden autorizovaný canary claim; `notification=null`
je fail-closed chyba, nikoli idle úspěch. Log musí obsahovat jen obecný
`delivery_finalized` s `outcome=sent`, žádný token, chat ID, UUID, URL, e-mail
ani HTTP body. Databázová acceptance musí vrátit přesně `t|t|t|t|t|0|0`:

```sql
SELECT
  canary.status = 'sent',
  canary.attempt_count = 1,
  canary.provider_message_id IS NOT NULL,
  canary.final_claim_token IS NOT NULL,
  canary.finalization_hash ~ '^[0-9a-f]{64}$',
  COUNT(*) FILTER (WHERE canary.status = 'authorized'),
  COUNT(*) FILTER (WHERE canary.status = 'claimed')
FROM public.b2b_telegram_handoff_canaries AS canary
GROUP BY canary.status, canary.attempt_count, canary.provider_message_id,
         canary.final_claim_token, canary.finalization_hash;
```

Současně ověřit `telegram_handoff_enabled=false`, nulový produkční ready/claimed
outbox a absenci `pending-finalize-v1.json` i circuit markeru. Canary marker
potom pouze archivovat; nemaže se durable DB ledger:

```bash
sudo mv /etc/freio-telegram-handoff/canary-enabled \
  /etc/freio-telegram-handoff/canary-enabled.used
sudo test ! -e /var/lib/private/freio-telegram-handoff/pending-finalize-v1.json
sudo test ! -e /var/lib/private/freio-telegram-handoff/circuit-open-v1.json
```

Teprve po této acceptance zapnout DB gate. Forward migrace obsahuje trigger,
který tento update odmítne, není-li singleton durable `sent`:

```sql
UPDATE public.b2b_autonomy_settings
SET telegram_handoff_enabled = TRUE
WHERE id = 1
  AND approval_status = 'approved'
  AND telegram_handoff_enabled IS FALSE
  AND telegram_handoff_daily_cap > 0
RETURNING telegram_handoff_enabled, telegram_handoff_daily_cap;
```

Nakonec vytvořit hlavní marker, provést jeden idle/produkční běh a zapnout oba
timery:

```bash
sudo install -o root -g root -m 000 /dev/null \
  /etc/freio-telegram-handoff/enabled
sudo systemctl start freio-telegram-handoff.service
sudo systemctl enable --now freio-telegram-handoff.timer
sudo systemctl start freio-telegram-handoff-health.service
sudo systemctl enable --now freio-telegram-handoff-health.timer
systemctl list-timers \
  freio-telegram-handoff.timer \
  freio-telegram-handoff-health.timer --no-pager
```

Pokud canary neskončí `sent`, ponechat hlavní gate `false`, oba timery vypnuté,
archivovat canary marker a zachovat pending/circuit i DB ledger pro
reconciliation. Singleton, claim ani starý `dead` řádek se nikdy ručně
neresetují.

## Chování chyb

- Timeout, nejednoznačný transport, HTTP 408/425 nebo 5xx po Telegram requestu
  se finalizuje jako `uncertain` a otevře circuit; stejná zpráva ani další claim
  se bez operátorské reconciliation znovu neposílá.
- Pouze jistě pre-send síťová nedostupnost a striktně validovaný Telegram
  `429` JSON envelope jsou retryable. `retry_after` se respektuje v rozsahu
  30 až 86400 sekund; nevalidní 429 je `uncertain` a otevře circuit.
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
sudo systemctl disable --now freio-telegram-handoff-health.timer
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
