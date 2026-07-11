# Typ: Python boti + statické/offline appky

**Projekty:** nate_trader, openClawTrader, teriProjekt. Většinou **nic nebo skoro
nic** k migraci — Supabase ani Resend tu nejsou.

---

## openClawTrader (⚪ už je to cílová architektura)

- **Už běží na self-hosted Postgres** (psycopg2 pool + `DATABASE_URL`, výchozí
  `postgresql://postgres:postgres@postgres:5432/openclaw`) + Redis + ChromaDB v
  `docker-compose.yml`. **Žádný Supabase, žádný Resend.**
- **Migrace = volitelná konsolidace:** buď nech běžet jeho vlastní Postgres kontejner
  na našem serveru (nejjednodušší), nebo přesměruj `DATABASE_URL` na náš sdílený
  Postgres (`newdb.sh openclaw`) a jeho PG kontejner zruš. Redis: buď jeho vlastní,
  nebo náš sdílený. ChromaDB nech jako kontejner.
- **Auth/e-mail:** žádné (ovládá se Telegramem). Nic neřešíš.
- Deploy: přenes `docker-compose.yml` na Dokploy (nebo `docker compose` na serveru).

## nate_trader (⚪ nic k migraci)

- **Žádná databáze** — stav je ploché JSON soubory v `state/` (+ `watchlist.json`).
  Žádný Supabase, žádný Postgres, žádný e-mail (notifikace jdou do **ClickUp**).
- **Migrace:** nic. Jen když bys chtěl centralizovat stav, šlo by přidat Postgres —
  ale nic to nevyžaduje.
- ⚠️ **Bezpečnost:** živé klíče (`ALPACA_*`, `PERPLEXITY_API_KEY`, `CLICKUP_*`) jsou
  commitnuté v `.env`. **Rotuj je** bez ohledu na migraci.
- Read-only Next.js dashboard (`dashboard/`) můžeš hodit na Dokploy jako statickou
  appku, když ho chceš mít u sebe.

## teriProjekt (⚪ N/A)

- Primárně **Flutter appka** (lokální **SQLite/Drift**, data na zařízení) + tenký
  **Next.js landing** wrapper na Vercelu. **Žádný Supabase, žádný e-mail, žádné
  cloud úložiště.**
- **Migrace:** nic serverového. Leda přesunout ten statický landing z Vercelu na
  Dokploy, když chceš „vše u nás" — ale funkčně netřeba.

---

## Poznámka k Python appkám obecně (dentallocal/explain-and-act/freio mají Python)

U Freio/dentallocal/explain-and-act jsou Python skripty **jen offline nástroje**
(generování obsahu přes Anthropic API, seed dat) — **neběží v runtime** a nemigrují
se. Spouštějí se lokálně a jejich výstup se seeduje do DB TS skripty. Do produkční
migrace nezasahují; jen ať `ANTHROPIC_API_KEY` pro ně zůstane dostupný lokálně.

## Univerzální ověření (tento typ)
- [ ] openClawTrader: `DATABASE_URL` míří kam má, bot běží, Telegram ovládání funguje
- [ ] Živé API klíče v `.env` botů rotovány a mimo git
- [ ] (volitelně) statické landingy přesunuty na Dokploy
