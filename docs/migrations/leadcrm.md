# LeadCRM — migrace DB + Storage na vlastní stack (plán, ne implementace)

## Verification notes

Ověřeno read-only proti zdrojům (`/home/anakin/programming/leadcrm/**`, `/home/anakin/programming/homelab/**`). Nalezené a opravené problémy:

1. **[KRITICKÉ — ztráta dat u cutoveru] Krok 8, krok „finální `--data-only` re-dump/restore … dorovná řádky z okna" je mechanicky rozbitý i koncepčně neúplný.** `pg_restore --data-only` dělá `COPY` (žádný upsert) → do už naplněné cílové DB (z Kroku 4) spadne na duplicitních klíčích (`leads.lead_key` a všechny PK). A i kdyby ne, aplikační re-sync (`/api/sync`) dorovná **jen `leads`** (upsert dle `lead_key`), **ne interaktivní tabulky** (`lead_cards` stage/position, `activities`, `tags`, `lead_tags`, `lead_sequence`, změny hesel v `app_users`). Cokoli, co během okna zapíšou uživatelé/tickery na Supabase (posun karty, poznámka, tag, změna hesla, webhookem řízený přesun do „replied"), by se **ztratilo**. **Oprava:** převzato z páteřního §7B — během okna **zmrazit zápisy na Supabase** (appka read-only/maintenance + pauza `/api/sync` cronu + pauza leadgen `/api/ingest` + pauza tickerů), pak **jeden finální plný `--data-only` dump** a **restore do ČERSTVÉ/PRÁZDNÉ** `leadcrm` DB (drop+recreate, nebo `TRUNCATE` všech 8 tabulek ve FK-bezpečném pořadí), ověřit počty, teprve pak přepnout env a rozmrazit. Restore z Kroku 4 je jen zkušební/„rozehřívací" — autoritativní je až ten zmražený u cutoveru. (Viz přepsaný Krok 8.)

2. **[BUG] Runbook používá aplikační `SUPABASE_URL` (REST URL `https://<ref>.supabase.co`) jako connection string pro `psql`/`pg_dump`.** To není platný libpq DSN → `psql "$SUPABASE_URL"` i `pg_dump "$SUPABASE_URL"` selžou. **Oprava:** zavést zvlášť `SUPABASE_DB_URL` (session pooler, port 5432, s heslem k DB) z `supabase/.temp/pooler-url`: `postgresql://postgres.wltyducohcornnrmijsy:<DB_HESLO>@aws-1-eu-central-1.pooler.supabase.com:5432/postgres`. Definovat i `LOCAL_URL` použitý v Kroku 7. (Opraveno napříč Kroky 1/3/4/7/8.)

3. **[bezpečnost/default] RLS default.** Spec nabízel „nech ENABLE" a „DISABLE" jako rovnocenné. **Doporučený default = nech ENABLED** (owner ho stejně obchází; kdyby se před sdílený Postgres kdy dostal PostgREST/Strategie A, ENABLED bez policy drží anon zamčený). `DISABLE` odebírá záchrannou síť bez přínosu.

4. **[přesnost] Počet call-site souborů.** Skutečně importuje `admin()` **9 souborů** (`queries.ts`, `actions.ts`, `users.ts`, `sequences.ts`, `inbox-pull.ts`, `imap.ts`, `sync.ts`, `api/ingest/route.ts`, `api/webhooks/launchmail/route.ts`) + `admin.ts` = **~10**, ne „~11". Odhad práce jinak sedí.

5. **[minor] `gen_random_uuid()` na PG13+ nevyžaduje `pgcrypto`** (je v jádře), ale `CREATE EXTENSION pgcrypto` je neškodný a `pg_trgm` je **nutný** (GIN trgm index `leads_name_trgm_idx`). Krok „create extension" ponechán.

6. **[minor] Verze Postgresu:** `.temp/postgres-version` = `17.6.1.127`, cíl `postgres:17` (stejný major → restore-safe). Uvedené „17.6" je OK.

**Ověřeno jako správné/bezpečné (potvrzeno v kódu):**
- **Triggery při restore neublíží.** `pg_dump`/`pg_restore` (custom format) odkládá triggery i FK do **post-data** sekce, restoreované až PO datech → `trg_create_card_for_lead` při `COPY` do `leads` **nevystřelí**, takže reálné `lead_cards` (se skutečnými stage) se přenesou 1:1, žádné falešné výchozí karty. (Toto by **neplatilo**, kdyby se re-runovaly migrace + vkládala data — plán správně jede dump/restore, ne přehrání migrací, a správně říká „0002 NEspouštět".)
- **Žádný `serial`/identity/sekvence** (grep = 0; všechny PK uuid/text) → „žádné `setval`" platí.
- **Žádná závislost na `auth.*`/GoTrue/RLS** v kódu ani migracích; jediný `@supabase/supabase-js` je service-role klient v `admin.ts` → Strategie B je authz-bezpečná. `APP_PASSWORD` je v `env.ts` definován, ale **nikde nevolán** (legacy). `AUTH_SECRET` správně označen nepřenositelný — pozor, `env.ts`/`middleware.ts` mají nebezpečný dev-fallback `leadcrm-dev-secret-change-me`, proto přenes reálnou hodnotu 1:1.
- **Split-brain analýza sedí:** jediné kanály leadgenu do `public.leads` viditelné z kódu leadcrmu jsou `/api/ingest` + Storage CSV (oba následují DB pointer); `lead_status` je leadgenu vlastní a leadcrm ho za běhu nečte → správně NEkopírovat. **Leadgen repo NENÍ na disku** → GATE je skutečně nutný a správně blokující.
- **Storage:** screenshoty se renderují přes prosté `<img>` (`next.config.ts` potvrzuje: žádné `images.remotePatterns`; žádné CSP hlavičky) → Storage-B pro leadcrm **nepotřebuje** změnu `next.config`/CSP (na rozdíl od obecného `storage-to-r2.md` kroku 4). Absolutní URL zapečené v `leads.screenshot_url`/`email_url` (`mapRow` ř. 71/73) potvrzeny → krok přepisu URL v DB je nutný a správný. `leads.raw` (celý CSV řádek JSONB) jede s DB dumpem → neztratí se.
- **LaunchMail integrace kompatibilní:** leadcrm volá `POST /api/mail/send`, `GET /api/incoming-emails`, `GET /api/logs` s `Authorization: Bearer`, webhook ověřuje `X-LaunchMail-Signature: sha256=<HMAC-SHA256>` — vše odpovídá homelab LaunchMail serveru (`basePath("/api")` + `/mail/send`, `/incoming-emails`, `/logs`; Bearer token middleware; `X-LaunchMail-Signature: sha256=…`). „E-mail beze změny" je v pořádku.
- **Strategie A (self-hosted Supabase) bezpečnostně sedí** (`self-hosted-supabase.md`): explicitně generovat vlastní `JWT_SECRET` (ne ukázkové defaulty), odvodit anon/service klíče, Studio jen přes Tailscale (nikdy veřejně), Postgres v compose bind jen `127.0.0.1:5432`. Jediná korekce: pro leadcrm A **není doslova „0 práce"** na infra — chce postavit PostgREST + storage-api + Kong nadrátované na JWT/service-role, které očekává stávající `SUPABASE_SERVICE_KEY`/`SUPABASE_URL`. „0 řádků kódu" platí, infra je netriviální.

---

> **Rozsah:** přesun **jen databáze** (Supabase Postgres → náš sdílený Postgres) a **jen Storage** (Supabase Storage bucket `leads` → Cloudflare R2). E-mail (LaunchMail) i deploy (Docker/Dokploy) jsou hotové a **neměníme je**. Auth je vlastní a **zůstává beze změny**.
>
> **Zdroj analyzován read-only.** Do `/home/anakin/programming/leadcrm` se během migrace NIKDY nepíše ani nepushuje z tohoto plánu — repo se mění jen svým vlastním deploy flow. Tenhle dokument je postup, ne akce.
>
> Navazuje na páteřní `supabase-postgres-data-migration.md`, `storage-to-r2.md`, `supabase-auth-to-better-auth.md`. Leadcrm je v `00-overview.md` označen jako **referenční vzor Strategie B**.

Sdílený Supabase projekt: ref `wltyducohcornnrmijsy`, region `eu-central-1`, Postgres `17.6.1.127` (major 17; z `supabase/.temp/*`). Pooler (session, port 5432): `aws-1-eu-central-1.pooler.supabase.com`.

---

## 0) TL;DR a klíčové rozhodnutí

- **DB:** čistá Strategie B (holý Postgres). Žádné RLS, žádný Supabase Auth, jen service-role. Ale **pozor**: appka dnes nemá `pg`/Drizzle ani `DATABASE_URL` — jede **100 % přes `supabase-js` (PostgREST)**. „Triviální repoint connection stringu" z `type-nextjs-drizzle-postgres.md` je u leadcrm optimistické: buď se napíše `pg` datová vrstva (**9 souborů call sites + `admin.ts`**), nebo se `supabase-js` nechá mířit na self-hosted PostgREST. Obě cesty popsané níž s odhadem práce.
- **Storage:** doporučuju **rozfázovat**. Nejdřív přesuň jen DB a `leads` bucket **nech na Supabase** (leadcrm ho jen čte). Teprve pak, jako samostatnou změnu, přesuň bucket na R2 — protože do bucketu **zapisuje leadgen, ne leadcrm**, takže je to dvoustranná změna (viz §1 a §6-Storage).
- **Největší riziko = sdílený projekt s leadgenem.** Řeší §1. Shrnutí: každý zapisovatel `public.leads` musí jít **skrz leadcrm** (`/api/ingest` nebo Storage CSV, který čte sync). `public.lead_status` patří leadgenu, leadcrm ho za běhu nepotřebuje → **nekopírovat, nechat na Supabase**.
- **Druhé největší riziko = cutover okno.** Interaktivní tabulky (karty, aktivity, tagy, sekvence, hesla) NEJDOU dorovnat aplikačním re-syncem. Proto cutover jede přes **zmražení zápisů + čerstvý restore do prázdné DB** (viz opravený Krok 8), ne „top-up".

---

## 1) ⚠️ SDÍLENÝ PROJEKT + LEADGEN — datová integrita (nejnebezpečnější část)

Sdílený Supabase projekt obsahuje tři světy:

| Vlastník | Objekt | Kdo zapisuje | Čte leadcrm? |
|---|---|---|---|
| **leadcrm** | `leads, stages, lead_cards, tags, lead_tags, activities, app_users, lead_sequence` + view `lead_board` + funkce `crm_*` | leadcrm (přes service key) **+ leadgen přes `/api/ingest`** | ano (běh) |
| **leadgen** | `public.lead_status` | leadgenův **starý dashboard `dashboard/web.mjs`** — přímý zápis do Postgresu | jen 1× (migrace `0002`, už proběhlo) |
| sdílené | Storage bucket **`leads`** (CSV, screenshoty PNG, e-maily `.txt`) | **leadgen worker** (píše) | ano (sync čte CSV; browser čte screenshot) |

### Jak leadgen „píše do leadcrm dat" — tři kanály

1. **HTTP `/api/ingest`** — leadgenův generátor volá `POST https://<leadcrm>/api/ingest?token=<WORKER_TOKEN>` s `{source, niche, rows[]}`. Endpoint (`src/app/api/ingest/route.ts`) dělá `admin().from("leads").upsert(..., { onConflict: "lead_key" })`. **Je to endpoint leadcrmu** → po migraci píše do té DB, na kterou leadcrm ukazuje. Dokud leadgen volá stejný leadcrm, **žádný split-brain**. ✅
2. **Storage CSV → sync** — leadgen worker nahraje `{src}/{niche}/leads.csv` do bucketu; leadcrm `/api/sync` (tlačítko/cron) to čte přes `runSync()` (`src/lib/sync.ts`) a upsertuje do `leads` (opět `onConflict: "lead_key"`). Koordinační bod jen při přesunu Storage (§6-Storage).
3. **Přímý zápis do DB (`lead_status`)** — jen leadgenův **starý web.mjs**, a jen do `lead_status`. Leadcrm tuto tabulku za běhu nepoužívá (potvrzeno grepem — §3, §4).

### Split-brain vzniká pouze tehdy, když…

…leadgen zapisuje **přímo do `public.leads` na Supabase** (mimo `/api/ingest` a mimo Storage). Pak by po přesunu leadcrmu tyto řádky přistály ve **staré** Supabase a v nové DB by chyběly → divergence / ztráta.

**Z kódu leadcrmu je vidět, že leadgenovy kanály do `leads` jsou pouze `/api/ingest` + Storage CSV** — obojí prochází leadcrmem, takže **následuje DB pointer**. Přímý DB-zapisovatel je jen `web.mjs` a jen do `lead_status`. **ALE leadgen repo NENÍ na disku** (`/home/anakin/programming/` ho neobsahuje — ověřeno), takže tohle je jediná věc, co **musíš ověřit v leadgenu** před cutoverem:

> **GATE (povinné před migrací):** V leadgen repu potvrď, že **žádný přímý Postgres connection (psql/`pg`/knex/…) nezapisuje do `public.leads`**. Grep na connection stringy a na `leads` / `insert`/`upsert`. Pokud takový zápis existuje, **musíš ho v cutover okamžiku přepnout na náš Postgres** současně s leadcrmem, jinak se data rozdvojí. Pokud leadgen píše `leads` výhradně přes `/api/ingest` + Storage CSV → migrace leadcrmu je bezpečná bez zásahu do leadgenu.

### Rozhodnutí o `lead_status` a o sdíleném projektu

- **`lead_status` NEKOPÍRUJ.** Leadcrm ho za běhu nečte (jen jednorázový import `0002`, dávno hotový). Kdybys ho zkopíroval do naší DB, **rozdvojíš ho** od stále běžícího `web.mjs`, který do Supabase verze dál píše. Nech ho žít na Supabase.
- **Nemigruj leadgen s sebou.** Není v rozsahu; jeho jediný přímý DB objekt (`lead_status`) leadcrm nepotřebuje. Varianta „přesuň oba" nemá výhodu a přidává riziko (musel bys přenést i `web.mjs`). **Vybraná varianta: přesun jen leadcrm tabulek** (Varianta 1 níž).

| Varianta | Co udělá | Konzistence | Verdikt |
|---|---|---|---|
| **1 — jen leadcrm DB** (doporučeno) | Přenes 8 leadcrm tabulek + view + funkce do naší DB. `lead_status` zůstane na Supabase. leadgen dál volá `/api/ingest` (→ nová DB) a píše Storage. | Bez split-brainu, **pokud** platí GATE výše. | ✅ |
| **2 — leadcrm + leadgen spolu** | Přenes i `lead_status` a přepni `web.mjs` na naši DB. | Vyžaduje zásah do leadgenu/web.mjs (mimo rozsah), víc pohyblivých dílů. | ❌ zbytečné |

---

## 2) Inventář databáze

Všechny migrace: `/home/anakin/programming/leadcrm/supabase/migrations/`.

### 2.1 Migrační soubory

| Soubor | Obsah |
|---|---|
| `0001_init.sql` | Extensions `pgcrypto`, `pg_trgm`. Tabulky `leads`, `stages` (+seed 7 řádků), `lead_cards`, `tags`, `lead_tags`, `activities`. Trigger funkce `create_card_for_lead()` (+ trigger `trg_create_card_for_lead` AFTER INSERT na `leads`) a `touch_card()` (+ `trg_touch_card` BEFORE UPDATE na `lead_cards`). `ENABLE ROW LEVEL SECURITY` na 6 tabulkách **bez policy**. View `lead_board`. |
| `0002_import_legacy_status.sql` | **Jednorázový** `DO` blok: import z `public.lead_status` (agreed→won, contacted→contacted, note→activity). Podmíněný `information_schema` checkem. **Referuje `lead_status`, ne `auth.*`.** Není to persistentní objekt (jen anonymní `DO`), takže se do schema dumpu nedostane. Při migraci NEPOUŠTĚT. |
| `0003_stats_rpc.sql` | Funkce `crm_stats()` (SQL `stable`, **`returns jsonb`**). Partial indexy `leads_has_email_idx`, `leads_has_web_idx`. |
| `0004_facets_rpc.sql` | Funkce `crm_facets()` (SQL, `stable`). |
| `0005_app_users.sql` | Tabulka `app_users`. `ENABLE RLS` bez policy. |
| `0006_activity_stats.sql` | Funkce `crm_activity_stats(p_days int default 14)`. Index `activities_created_idx`. |
| `0007_owner.sql` | `lead_cards.owner` + index `lead_cards_owner_idx`. Re-create `lead_board` (+owner). |
| `0008_email_status.sql` | `lead_cards.email_status`, `email_checked_at`. Re-create `lead_board`. |
| `0009_sequences.sql` | Tabulka `lead_sequence` + index `lead_sequence_due_idx`. `ENABLE RLS`. |
| `0010_ares.sql` | `lead_cards.ico`, `founded_year`, `company_active` + index `lead_cards_ico_idx`. Re-create `lead_board` (**finální definice view**). |
| `0011_duplicates.sql` | Funkce `crm_duplicate_groups(p_limit int default 200)` → `returns table`. |
| `0012_niche_stats.sql` | Funkce `crm_niche_stats()` → `returns table`. |
| `0013_perf_indexes.sql` | Indexy `leads_ranking_idx`, `lead_cards_next_action_idx`, `activities_type_created_idx`, `activities_meta_to_idx`. `ANALYZE`. |

### 2.2 Tabulky (8) — všechny v `public`

- **`leads`** — PK `id uuid default gen_random_uuid()`, `lead_key text unique`, business sloupce, `flags text[]`, `raw jsonb default '{}'`, `first_seen`, `last_synced`.
- **`stages`** — PK `id uuid`, `key unique`, `label`, `color`, `position int`, `is_won`, `is_lost`. Seed: `new, contacted, replied, negotiating, won, lost, has_web`. (Pozn.: seed `has_web` má `is_lost=true`.)
- **`lead_cards`** — PK `lead_id uuid → leads(id) ON DELETE CASCADE`; `stage_id → stages(id) ON DELETE SET NULL`; `position double precision`; `next_action_at`; `updated_at`; + přidané `owner`, `email_status`, `email_checked_at`, `ico`, `founded_year`, `company_active`.
- **`tags`** — PK `id uuid`, `name unique`, `color`.
- **`lead_tags`** — PK `(lead_id, tag_id)`, obě FK CASCADE.
- **`activities`** — PK `id uuid`, `lead_id → leads(id) CASCADE`, `type text`, `body`, `meta jsonb`, `actor`, `created_at`.
- **`app_users`** — PK `username text` (**ne UUID**), `password_hash`, `display_name`, `is_admin`, `created_at`, `updated_at`.
- **`lead_sequence`** — PK `lead_id → leads(id) CASCADE`, `step int`, `next_due_at`, `status`, `stopped_reason`, `updated_at`.

### 2.3 View: `lead_board`

Finální def v `0010_ares.sql` (řádky 9-28): `leads.*` + `stage_id, card_position, next_action_at, card_updated_at, stage_key, stage_label, stage_color, stage_position, owner, email_status, email_checked_at, ico, founded_year, company_active`. LEFT JOIN `lead_cards` a `stages`. **Referuje jen leadcrm tabulky. Bez `auth.*`. Plně portovatelný.**

### 2.4 Funkce (přes `.rpc()`) — portabilita

| Funkce | Jazyk / návrat | Referuje `auth.*`? | Portovatelné na holý PG? |
|---|---|---|---|
| `crm_stats()` | SQL `stable`, `returns jsonb` | ne | ✅ ano |
| `crm_activity_stats(p_days int)` | SQL `stable` | ne | ✅ ano |
| `crm_niche_stats()` | SQL `stable`, `returns table` | ne | ✅ ano |
| `crm_duplicate_groups(p_limit int)` | SQL `stable`, `returns table` | ne | ✅ ano |
| `crm_facets()` | SQL `stable` | ne | ✅ ano |
| `create_card_for_lead()` (trigger) | plpgsql | ne | ✅ ano |
| `touch_card()` (trigger) | plpgsql | ne | ✅ ano |

**Všechny jsou čisté SQL/plpgsql nad `public.*`, žádná závislost na GoTrue/`auth.*`.** Přenesou se v schema dumpu 1:1.

### 2.5 Triggery, enumy, indexy, extensions, RLS

- **Triggery:** `trg_create_card_for_lead` (nový lead → auto karta v nejnižším ne-lost stage), `trg_touch_card` (auto `updated_at`).
  - **Bezpečnost při restore:** `pg_dump`/`pg_restore` (custom format) odkládají triggery + FK do **post-data** sekce → při `COPY` dat do `leads` trigger NEvystřelí, takže reálné `lead_cards` (se skutečnými stage) se zachovají 1:1. **Nikdy nemigruj přehráním migrací + vkládáním dat** (to by trigger vytvořil falešné karty a `ON CONFLICT DO NOTHING` by přepsal skutečné). Jede se výhradně dump/restore.
- **Enumy:** **žádné.** Všechny „statusy" jsou `text`. Nic k portování.
- **Sekvence / identity:** **žádné** (grep = 0; všechny PK `uuid`/`text`) → žádné `setval`/`nextval` řešit.
- **Indexy:** `leads_source_niche_idx`, `leads_verdict_idx`, `leads_name_trgm_idx` (GIN `gin_trgm_ops` → **vyžaduje `pg_trgm`**), `lead_cards_stage_idx`, `activities_lead_idx`, `leads_has_email_idx`, `leads_has_web_idx`, `activities_created_idx`, `lead_cards_owner_idx`, `lead_sequence_due_idx`, `lead_cards_ico_idx`, `leads_ranking_idx`, `lead_cards_next_action_idx`, `activities_type_created_idx`, `activities_meta_to_idx` (partial na `type='email_sent'`).
- **Extensions (musí být na cíli, jinak restore GIN indexu spadne):** `pg_trgm` (trgm GIN index — **nutný**), `pgcrypto` (na PG13+ není striktně nutný pro `gen_random_uuid()`, které je v jádře, ale neškodí; vytvoř ho také). Obě standardní v `postgres:17`.
- **RLS:** `ENABLE` na všech tabulkách, **ale bez jediné policy**. Na holém PG se appka připojuje jako **owner role `leadcrm`** (viz `newdb.sh`) → owner RLS obchází (**není `FORCE`** — ověřeno). **Default: nech `ENABLE` beze změny.** (Je to záchranná síť, kdyby se před sdílený PG kdy dostal PostgREST; owner ji stejně nevidí. `DISABLE` je zbytečné a odebírá obranu.) **Žádná appka nespoléhá na RLS pro autorizaci.**

---

## 3) `supabase-js` call sites a klasifikace

Jediný import `@supabase/supabase-js` je v `src/lib/supabase/admin.ts` (`createClient(url, service_key)`). Klient se používá čistě jako **service-role PostgREST + Storage REST**. Žádný `supabase.auth.*`, žádné RLS. (`admin()` importuje 9 souborů — viz níž.)

### 3.1 Přehled call sites

**`.rpc()` (5, vše v `src/lib/queries.ts`):** `crm_stats` (ř. 98), `crm_activity_stats` (ř. 122), `crm_niche_stats` (ř. 407), `crm_duplicate_groups` (ř. 434), `crm_facets` (ř. 444). — všechny ověřeny.

**`.from()` (DB) podle souboru:**
- `src/lib/queries.ts` — `lead_board`, `leads`, `stages`, `tags`, `lead_tags`, `activities` (čtení, county, stránkování, embedded join `lead_tags → tags(*)` ř. 69).
- `src/lib/actions.ts` — `lead_cards`, `activities`, `stages`, `tags`, `lead_tags`, `leads` (mutace + čtení).
- `src/lib/users.ts` — `app_users` (seed/select/update/upsert).
- `src/lib/sequences.ts` — `lead_sequence`, `lead_board`, `activities`.
- `src/lib/inbox-pull.ts` — `activities`, `leads`, `stages`, `lead_cards`.
- `src/lib/imap.ts` — `activities`, `stages`, `lead_cards`.
- `src/lib/sync.ts` — `leads` (upsert).
- `src/app/api/ingest/route.ts` — `leads` (upsert; **leadgen kanál**).
- `src/app/api/webhooks/launchmail/route.ts` — `activities`, `leads`, `stages`, `lead_cards`.

> Route handlery `api/sequences/tick`, `api/imap/poll`, `api/inbox/pull`, `api/export`, `api/sync` volají výše uvedené lib funkce, **admin() přímo neimportují** → při přepisu se nemění.

**Storage REST (mimo supabase-js SDK — ruční `fetch`):**
- `src/lib/sync.ts` `listFolder()` — `POST {SUPABASE_URL}/storage/v1/object/list/{bucket}` s `apikey` + `Bearer` service key.
- `src/lib/sync.ts` `fetchText()` — `GET publicUrl(path)` (veřejný objekt, čte `leads.csv`).
- `src/lib/supabase/admin.ts` `publicUrl(path)` — string builder `{SUPABASE_URL}/storage/v1/object/public/{bucket}/{enc}` (ř. 18-22).

**PostgREST idiomy, které při přepisu na `pg` chtějí pozornost:** embedded resource `select("lead_id, tags(*)")`, jsonb filtry `.eq("meta->>to", …)` a `.in("meta->>incoming_id", ids)`, compound `.or("email.is.null,email.eq.,email_status.eq.invalid")`, `.not("stage_key","in","(won,lost,replied,has_web)")`, `.upsert(..., { onConflict, ignoreDuplicates })`, `count: "exact", head: true`, `.range()` stránkování, `.single()`. **Navíc:** dotazy v `queries.ts` jsou obalené `unstable_cache` + invalidace `revalidateTag` (CACHE_TAGS) — při přepisu na `pg` cache-vrstvu zachovej (jinak se drahé RPC/facety pouští při každém renderu).

### 3.2 Co se musí přepsat vs. co lze zachovat

| Cesta | Zásah do kódu | Poznámka |
|---|---|---|
| **A — self-hosted Supabase** (PostgREST + storage-api + Kong nad naším Postgresem) | **Nulový v appce.** Jen přepni `SUPABASE_URL` + service key. | `.from/.rpc/.storage` fungují dál. **Infra netriviální:** musíš postavit PostgREST + storage-api + Kong nadrátované na JWT/service-role, které očekává stávající `SUPABASE_SERVICE_KEY`. Viz `self-hosted-supabase.md`. |
| **B — holý `pg`/Drizzle** | **Přepiš 9 souborů** call sites + `admin.ts`. RPC volej `SELECT … FROM crm_*(...)` (u `crm_stats()` `SELECT crm_stats()` → čti `rows[0].crm_stats`). | Čisté (bez RLS, jen service key → žádné riziko „obnažení" dat). Napiš tenkou vrstvu `db.query(...)` místo `admin()`. |

### 3.3 Doporučení a odhad práce

Leadcrm **nemá žádnou RLS závislost a používá jen service key** → **plný přepis na `pg` je čistý a bezpečný** (nehrozí prosáknutí dat kvůli chybějícímu authz filtru, protože žádný per-user filtr neexistuje — vše je service-level). Proto **doporučuju Strategii B** (odpovídá roli „referenční vzor" v `00-overview.md`).

**Odhad práce (B):** appka dnes `pg`/Drizzle ani `DATABASE_URL` nemá, takže to není „změna jednoho stringu", ale skutečný přepis datové vrstvy:
- Napsat `src/lib/db.ts` (pool nad `pg` / `postgres.js`) a helper, který nahradí `admin()`; zachovat `unstable_cache` obalování.
- Přepsat **9 souborů** (+`admin.ts`), mechanicky. Nejpracnější: embedded join `tags(*)` (rozdělit na 2. dotaz nebo `json_agg`), jsonb `meta->>` filtry, `.or()`/`.not(...in...)`, `upsert onConflict` (`INSERT … ON CONFLICT … DO UPDATE`), `count exact` (samostatný `count(*)`).
- RPC zůstávají v DB → `SELECT * FROM crm_*()`.
- **Odhad: ~1-2 dny** soustředěné práce + smoke testy. Riziko nízké (žádné authz jemnosti).

**Rychlejší alternativa, když spěcháš:** Strategie A (self-hosted Supabase) = **0 řádků kódu appky**, jen repoint (ale netriviální infra). Rozumné jako mezikrok; později přejít na B.

> Ať B nebo A: **endpoint `/api/ingest` běží dál beze změny** → leadgen se nedotkne, dokud má stejný `WORKER_TOKEN` a stejnou leadcrm doménu.

---

## 4) Auth — potvrzení (migrace triviální, Better Auth NETŘEBA)

Potvrzeno **plně vlastní auth, bez Supabase**:

- **Úložiště:** `public.app_users` (`0005_app_users.sql`), PK = **`username` (text, ne UUID)**.
- **Hesla:** PBKDF2-SHA256 přes Web Crypto v `src/lib/passwords.ts`, formát `pbkdf2$<iter>$<saltB64>$<hashB64>`, **120 000 iterací**, keylen 32. Seed v `src/lib/users.ts` (default účty + volitelně `APP_USERS`).
- **Session:** HMAC-SHA256 podepsaná cookie `leadcrm_session` (`src/lib/session.ts` + `src/lib/auth.ts`); token = `base64url(username).hmac`, klíč `AUTH_SECRET`. Middleware `src/middleware.ts` ověřuje cookie (public paths: `/login`, `/api/login`, `/api/sync`, `/api/webhooks`, `/api/sequences`, `/api/imap`, `/api/inbox`, `/api/ingest`).
- **Grep na `auth.users` / `auth.uid` / `gotrue` / `supabase.auth` = prázdný.** Jediný `@supabase` import je service-role klient.
- **Žádné UUID→auth-user FK, žádná RLS závislost, žádné `auth.users`.**

**Důsledek:** účty jedou s `pg_dump`em tabulky `app_users` (hashe se přenášejí 1:1, žádné resety). **Nutné: přenést `AUTH_SECRET` 1:1** — jinak se všechny existující cookies zneplatní (uživatelé se musí znovu přihlásit; **není to ztráta dat**, jen nepohodlí).
> ⚠️ Pozor: `env.ts` (`authSecret()`) i `middleware.ts` mají **nebezpečný dev-fallback `"leadcrm-dev-secret-change-me"`** — v Dokploy env MUSÍ být reálný `AUTH_SECRET` nastaven (a přenesen ze staré instance beze změny), jinak appka podepisuje cookies slabým veřejně známým klíčem.

`AUTH_SECRET` je v `00-overview.md` v seznamu nepřenositelných klíčů. `APP_PASSWORD` je v `env.ts` definován, ale **nikde nevolán** (legacy, neškodné). **Better Auth se nezavádí.** ✅

---

## 5) Storage — bucket `leads`, `publicUrl`, a co je (ne)v Postgresu

### 5.1 Co bucket obsahuje a kdo ho používá

Struktura (z `mapRow`/`runSync` v `src/lib/sync.ts`): `{source}/{niche}/leads.csv`, `{source}/{niche}/<screenshot>` (PNG), `{source}/{niche}/emails/<slug(name)>.txt`.

- **Zapisuje:** leadgen worker. **Leadcrm nikdy nepíše do Storage** (žádné `.upload`/`PutObject` — grep `.storage` = 0).
- **Čte leadcrm:** `runSync()` listuje složky + stahuje `leads.csv` (server); browser načítá `screenshot_url` jako prosté `<img>` (`src/app/(app)/leads/[id]/page.tsx` ř. 152-161). `email_url` je jen sloupec — **v UI se nikde nerenderuje** (grep potvrzen; cold/follow-up e-mail se generuje za běhu z polí leadu, ne ze Storage). `publicUrl` je jediný „writer" URL (string builder, `admin.ts` ř. 18-22).

### 5.2 KRITICKÉ: URL jsou zapečené v DB jako absolutní

`mapRow` (ř. 71, 73) ukládá do `leads.screenshot_url` a `leads.email_url` **plné absolutní URL** postavené `publicUrl()` v čase sync/ingest, tj. `https://<ref>.supabase.co/storage/v1/object/public/leads/...`. Přesun Storage → R2 tedy vyžaduje **oboje**:
1. přepsat base v `publicUrl()` (a v `listFolder()`) na R2, a
2. buď **přepsat existující URL v DB** (`UPDATE leads SET screenshot_url = replace(...)`, totéž `email_url`), **nebo** re-run sync/ingest (mapRow URL přepíše).

> **Bonus pro leadcrm:** screenshoty jdou přes prosté `<img>`, `next.config.ts` nemá `images.remotePatterns` ani CSP hlavičky → Storage-B pro leadcrm **NEpotřebuje** krok „uprav next.config/CSP" z obecného `storage-to-r2.md` (§4). Stačí přepis base v kódu + přepis URL v DB.

### 5.3 Kvantifikace „blob-ish dat i v Postgresu"

| Artefakt | V Postgresu? | Ve Storage? |
|---|---|---|
| **Raw CSV řádek** (celý objekt firmy) | **✅ ANO** — `leads.raw jsonb` (`mapRow`: `raw: o`). Kompletní řádek per lead. | zdrojový `leads.csv` |
| **Screenshot (PNG)** | **NE** — jen URL v `leads.screenshot_url`. Binárka jen ve Storage. | ✅ |
| **E-mail `.txt`** | **NE** — jen URL v `leads.email_url` (a i ta se v UI nepoužívá). | ✅ |
| `activities.meta jsonb` | drobná strukturovaná metadata (`message_id`, `to`, `status`…), ne blob | — |

**Závěr:** jediné „blob-ish" v Postgresu je **`leads.raw`** — a ten **jede s DB dumpem**, takže o něj při přesunu nepřijdeš. Screenshoty a e-mailové `.txt` jsou **jen ve Storage** (v DB jen URL). View `lead_board` sice `leads.raw` vystavuje (přes `l.*`), ale seznamové dotazy ho záměrně nevybírají (`LIST_COLS` v `queries.ts` ř. 18-19); `select *` (detail/export) ho vrací.

> Pozn.: tabulka ve `storage-to-r2.md` píše „HTML těl mailů" — kód ve skutečnosti odkazuje **`.txt`** soubory (`emails/<slug>.txt`). Drobná nepřesnost dokumentu, funkčně stejné.

---

## 6) ENV proměnné (z `/home/anakin/programming/leadcrm/.env.example` + `env.ts`)

**Používané dnes:**
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET` (default `leads`), `AUTH_SECRET` (fallback dev — přenes reálný!), `APP_USERS` (volitelné, seed), `LAUNCHMAIL_API_URL` (default `https://relay.launchday.cz`), `LAUNCHMAIL_API_KEY`, `MAIL_FROM` (volitelné), `MAIL_REPLY_TO` (volitelné), `LAUNCHMAIL_WEBHOOK_SECRET`, `DAILY_EMAIL_CAP` (default 200), `WORKER_TOKEN`, `SYNC_TOKEN`, `ENABLE_IMAP_POLL`, `IMAP_HOST`, `IMAP_PORT` (default 993), `IMAP_USER`, `IMAP_PASS`, `DISABLE_WORKERS`.

- `env.ts` navíc definuje `appPassword() = req("APP_PASSWORD")`, ale **`APP_PASSWORD` se v kódu nikde nevolá** — legacy, neškodné.
- `workerToken()` = `WORKER_TOKEN ?? SYNC_TOKEN` → oba autorizují `/api/ingest` i tickery.

**Přibydou při Strategii B (DB):** `DATABASE_URL` (pooled `shared-pgbouncer:6432`), `DIRECT_URL` (`shared-postgres:5432`) — z výstupu `newdb.sh leadcrm`.

**Přibydou při Storage → R2 (varianta S-B):** `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, veřejná R2 doména pro `publicUrl` base. (Při S-A zůstávají `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`/`SUPABASE_BUCKET` jen kvůli Storage.)

**Pomocné pro runbook (NEjde do appky, jen do shellu migrátora):**
- `SUPABASE_DB_URL` = **session pooler Postgres DSN** (port 5432, s heslem k DB, NE aplikační REST `SUPABASE_URL`): `postgresql://postgres.wltyducohcornnrmijsy:<DB_HESLO>@aws-1-eu-central-1.pooler.supabase.com:5432/postgres`.
- `LOCAL_URL` = `postgres://leadcrm:<HESLO>@localhost:5432/leadcrm` (přes ssh tunel).

**Beze změny (e-mail hotový):** všechny `LAUNCHMAIL_*`, `IMAP_*`, `DAILY_EMAIL_CAP`, `WORKER_TOKEN`, `SYNC_TOKEN`, `AUTH_SECRET`.

---

## 7) Runbook (plán — leadcrm repo se z tohoto NIKDY nemění/nepushuje)

Metodika: `supabase-postgres-data-migration.md`. Zlaté pravidlo: **Supabase zůstává živý jako fallback, dokud není cíl ověřený a pár dní v provozu.**

> ⚠️ **Ve všech `psql`/`pg_dump`/`pg_restore` používej `SUPABASE_DB_URL` (Postgres DSN, port 5432), NE aplikační `SUPABASE_URL` (to je REST endpoint `https://<ref>.supabase.co` a jako libpq DSN NEfunguje).**

### Krok 0 — GATE (leadgen)
Ověř v leadgen repu, že **`public.leads` nezapisuje žádný přímý DB connection** (jen `/api/ingest` + Storage CSV). Bez tohoto potvrzení **nepokračuj** (viz §1). Zároveň zjisti, zda leadgen umí repointnout Storage cíl (potřeba jen pro variantu S-B).

### Krok 1 — enumeruj a klasifikuj `public.*` na Supabase
```bash
psql "$SUPABASE_DB_URL" -c "\dt public.*"
```
Očekávané leadcrm tabulky: `leads, stages, lead_cards, tags, lead_tags, activities, app_users, lead_sequence`. Vše ostatní (min. `lead_status`, případně další leadgenovy) → **vyloučit z dumpu**. Sestav `--exclude-table` seznam.

### Krok 2 — provisioning cílové DB
```bash
ssh anakin@homelab
/srv/homelab/scripts/newdb.sh leadcrm        # role + DB + heslo, vypíše DATABASE_URL/DIRECT_URL
sudo docker exec -it shared-postgres psql -U postgres -d leadcrm \
  -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS pg_trgm;'
```
Ulož heslo do `homelab/secrets/` (gitignored), ne do gitu. (`newdb.sh` vytvoří i `pg_stat_statements` a `REVOKE CONNECT … FROM PUBLIC`.)

### Krok 3 — dump (read-only, na Supabase nic nemění)
```bash
# schéma + data leadcrm tabulek; VYLUČ leadgenovy (lead_status atd.)
pg_dump "$SUPABASE_DB_URL" --format=custom --no-owner --no-privileges \
  --schema=public \
  --exclude-table='public.lead_status' \
  # + další leadgenovy tabulky z Kroku 1 (každou jako další --exclude-table)
  -f leadcrm_public.dump
```
Dump nese i view `lead_board`, funkce `crm_*` a trigger funkce (v post-data sekci). `0002` je jen anonymní `DO` blok → v dumpu není; NEspouštět. Žádné `serial`/identity → **žádné `setval`**.

### Krok 4 — restore do naší DB (**zkušební** — autoritativní je až Krok 8)
```bash
ssh -L 5432:localhost:5432 anakin@homelab   # tunel
pg_restore --no-owner --no-privileges --role=leadcrm --schema=public \
  -d "$LOCAL_URL" --jobs=4 leadcrm_public.dump
```
Chyby o chybějícím `lead_status` neočekávej (view/funkce ho nereferují — ověřeno). RLS: **nech `ENABLE`** (owner obchází). Triggery se díky post-data sekci nespouští během dat → `lead_cards` se přenesou 1:1.

### Krok 5 — Storage (fázově)

**Fáze Storage-A (doporučeno první): nech bucket na Supabase.**
- Leadcrm dál čte Storage přes stávající `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` + `SUPABASE_BUCKET`. `publicUrl`/`listFolder` beze změny. `screenshot_url` v DB zůstávají platné. **Nulová koordinace s leadgenem.** DB je plně na našem PG, Storage odloženo.

**Fáze Storage-B (později, samostatná změna): bucket → R2.** Protože do bucketu **píše leadgen**, je to dvoustranné:
1. `rclone copy` bucket `leads` → `r2:leadcrm-leads` (viz `storage-to-r2.md`), `rclone check`.
2. R2 bucket zveřejni (custom doména) pro `publicUrl`.
3. Přepiš base v `publicUrl()` + `listFolder()` na R2 (S3 klient / veřejná doména). **next.config/CSP se u leadcrm neřeší** (prosté `<img>`, žádné CSP hlavičky).
4. **Přepiš existující URL v DB:**
   ```sql
   UPDATE leads SET screenshot_url = replace(screenshot_url,
     'https://<ref>.supabase.co/storage/v1/object/public/leads/','https://<r2-domena>/')
   WHERE screenshot_url LIKE '%supabase.co/storage%';
   -- totéž email_url
   ```
   nebo re-run sync/ingest (mapRow URL přepíše).
5. **Přepni leadgen worker, aby psal CSV/screenshoty do R2**, a při cutoveru dojeď finální `rclone copy` (dorovná soubory zapsané během okna). Bez tohoto kroku by nové leadgen soubory chodily do staré Supabase Storage → leadcrm by je z R2 neviděl.

### Krok 6 — kód/config a repoint
- **Strategie B:** naše `pg` vrstva místo `admin()`, přepsaných 9 souborů, RPC přes `SELECT … FROM crm_*()`; zachovej `unstable_cache`. Env: přidej `DATABASE_URL`/`DIRECT_URL`. Zachovej `AUTH_SECRET`, `LAUNCHMAIL_*`, `WORKER_TOKEN`, `SYNC_TOKEN`, `IMAP_*`, `DAILY_EMAIL_CAP`. Storage env dle fáze (S-A: ponech `SUPABASE_*`; S-B: `R2_*`).
- **Strategie A:** repoint `SUPABASE_URL` + service key na self-hosted Kong; kód appky beze změny.
- Změny nasaď **výhradně přes vlastní deploy flow leadcrmu** (Dokploy), **ne z tohoto plánu**.

### Krok 7 — verifikace (nepřeskakuj)
```bash
# a) počty řádků zdroj vs cíl (jen leadcrm tabulky)
for t in leads stages lead_cards tags lead_tags activities app_users lead_sequence; do
  s=$(psql "$SUPABASE_DB_URL" -Atc "SELECT count(*) FROM public.$t");
  d=$(psql "$LOCAL_URL"       -Atc "SELECT count(*) FROM public.$t");
  [ "$s" = "$d" ] && echo "OK $t $s" || echo "MISMATCH $t supabase=$s local=$d";
done
```
- **JSONB blob:** `SELECT count(*) FROM leads WHERE raw <> '{}'::jsonb;` musí sedět zdroj vs cíl (potvrzuje přenos `leads.raw`).
- **Funkce/view:** `SELECT crm_stats();`, `SELECT * FROM crm_facets();`, `SELECT count(*) FROM lead_board;` běží bez chyb.
- **FK integrita:** žádné osiřelé `lead_cards`/`activities`/`lead_tags`/`lead_sequence` bez `leads`.
- **Auth:** login existujícím `app_users` účtem projde (hashe + `AUTH_SECRET` sedí).
- **leadgen koexistence:**
  - Testovací `POST /api/ingest?token=<WORKER_TOKEN>` s 1 řádkem → řádek přistál v **nové** DB (ne v Supabase).
  - `POST /api/sync` → čte Storage, upsertuje do nové DB, `errors: []`.
  - `lead_status` na Supabase **beze změny** (nedotčeno; `web.mjs` klidně běží dál).
- **Storage:** screenshot v detailu leadu se načte z platné URL (S-A: Supabase; S-B: R2).
- Ulož výstup porovnání počtů jako důkaz před cutoverem.

### Krok 8 — cutover (opraveno: zmražení + čerstvý restore do prázdné DB)

> **Proč ne „top-up":** `pg_restore --data-only` dělá `COPY` (žádný upsert) → do už naplněné DB spadne na duplicitních PK. A aplikační re-sync dorovná **jen `leads`**, ne interaktivní tabulky (`lead_cards`, `activities`, `tags`, `lead_tags`, `lead_sequence`, hesla v `app_users`). Proto se cutover dělá se **zmraženými zápisy** a **plným restorem do prázdné cílové DB**.

1. **Zmraž zápisy na Supabase (krátké maintenance okno):**
   - Přepni leadcrm do **read-only / maintenance** (nebo appku dočasně zastav), aby uživatelé nezapisovali.
   - **Pozastav `/api/sync` cron**, **leadgen `/api/ingest`** (dle GATE) a **interní tickery** (`DISABLE_WORKERS=true` nebo pozastav cron sekvencí/imap/inbox), aby nevznikaly nové `activities`/přesuny karet na Supabase.
2. **Vyprázdni cílovou DB** (zahoď zkušební restore z Kroku 4):
   ```bash
   psql "$LOCAL_URL" -c "TRUNCATE app_users, lead_sequence, lead_tags, activities, lead_cards, tags, stages, leads RESTART IDENTITY CASCADE;"
   # (nebo jednodušeji: dropdb+createdb leadcrm a znovu Krok 2 extensions)
   ```
3. **Finální plný dump ze zmražené Supabase → restore do prázdné DB:**
   ```bash
   pg_dump "$SUPABASE_DB_URL" --format=custom --no-owner --no-privileges \
     --schema=public --exclude-table='public.lead_status' \
     # + další leadgenovy exclude
     -f leadcrm_final.dump
   pg_restore --no-owner --no-privileges --role=leadcrm --schema=public \
     -d "$LOCAL_URL" --jobs=4 leadcrm_final.dump
   ```
   (Schéma je už z prázdné DB pryč, pokud jsi dropnul; pokud jsi jen `TRUNCATE`oval, přidej `--data-only` — ale pak cílová DB **musí** být prázdná, což `TRUNCATE` zajistil.)
4. **Ověř počty** (Krok 7) na čerstvých datech — musí sedět zdroj (zmražený) vs cíl.
5. **Přepni leadcrm env** (DB, příp. Storage) a **redeploy** (Dokploy).
6. **Rozmraz:** odmrazi cron/tickery a (dle GATE) leadgen `/api/ingest`; volitelně **re-run `/api/sync`** (idempotentně dorovná cokoli nového od zmražení).
7. **Smoke test:** login, board, detail (screenshot), přesun karty, poznámka, odeslání e-mailu přes LaunchMail, `/api/ingest` test.
8. **Supabase NEMAZAT** — nech read-only jako fallback ≥ 1-2 týdny.

> Pozn.: `leads` je díky idempotentnímu upsertu (`lead_key`) samo o sobě odpouštivé, ale interaktivní tabulky ne — proto zmražení. Pokud chceš úplně bez okna, jde logická replikace (`CREATE SUBSCRIPTION`), ale pro leadcrm je krátké okno jednodušší a bezpečnější.

### Krok 9 — po cutoveru
- Leadcrm je automaticky v noční záloze (`scripts/backup.sh` dumpuje každou DB `pg_dump -Fc` do `r2:homelab-backups/YYYY-MM/`) → ověř `rclone ls r2:homelab-backups | grep leadcrm`.
- **Rotuj `SUPABASE_SERVICE_KEY`**, pokud byl v gitu; nová hesla jen v Dokploy env / `secrets/`.
- `AUTH_SECRET` **ponech** (jinak vynucené re-loginy).
- Zkontroluj slow-query log (`log_min_duration_statement=500`); leadcrm má spoustu partial/GIN indexů (`0013`) — ověř, že se přenesly (jsou v post-data schema dumpu).

### Rollback
Supabase je netknutý (jen jsme z něj četli). **Přepni leadcrm env zpět** na Supabase (DB, příp. Storage) a redeploy. Případný delta zápisů z krátkého okna zůstal na Supabase (zmražení bylo jen na leadcrmu), takže návratem se nic neztratí. Zdroj se maže až po prokázaně bezchybném provozu + záloze v R2.

---

## 8) Checklist „done"
- [ ] **GATE:** leadgen nezapisuje `public.leads` přímo do Supabase (jen `/api/ingest` + Storage CSV).
- [ ] Extensions `pg_trgm` (nutný) + `pgcrypto` v naší `leadcrm` DB.
- [ ] Runbook používá `SUPABASE_DB_URL` (pooler 5432), NE aplikační `SUPABASE_URL`.
- [ ] Cutover přes **zmražení zápisů + plný restore do prázdné DB** (ne `--data-only` top-up).
- [ ] Počty řádků zdroj == cíl (8 tabulek); `leads.raw` blob count sedí.
- [ ] `crm_*` funkce + `lead_board` běží na našem PG; indexy z `0013` přeneseny.
- [ ] `app_users` login funguje bez resetů; `AUTH_SECRET` přenesen 1:1 (ne dev-fallback).
- [ ] `/api/ingest` (leadgen) i `/api/sync` píší do **nové** DB; `lead_status` na Supabase nedotčeno.
- [ ] Screenshot v detailu se načítá (Storage S-A na Supabase, nebo S-B na R2 + přepsané URL).
- [ ] E-maily jdou dál přes LaunchMail (`/api/mail/send` Bearer — beze změny).
- [ ] leadcrm v noční R2 záloze; Supabase ponechán jako fallback ≥ 1-2 týdny.
- [ ] `SUPABASE_SERVICE_KEY` rotován, pokud byl v gitu.
