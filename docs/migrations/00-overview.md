# Migrace všech projektů na vlastní stack — MASTER PŘEHLED

Cíl: přesunout všechny osobní projekty z Vercelu/Railway + Supabase + Resend na
**náš domácí server** (Dokploy + náš Postgres + náš Auth + LaunchMail + R2), **bez
ztráty jediného řádku dat** a bez výpadku živých projektů (hlavně **Freio** —
produkce s klienty a spoustou dat).

> Tyto dokumenty jsou **plán, ne implementace.** Nic se zatím nemění. Čti je jako
> pečlivý postup, podle kterého to pak budeme dělat projekt po projektu.

---

## ⚠️ Dvě opravy oproti původním domněnkám (důležité)

1. **Žádný projekt nepoužívá Clerk.** Auth je všude buď **Supabase Auth (GoTrue)**,
   nebo vlastní (leadcrm), nebo žádný (hummy, boti). Původní „Clerk" byl
   false-positive grep (řetězec v quiz-datech).
2. **Freio nemá Prisma.** Schéma je **ručně psané raw SQL** v `supabase/*.sql`.

---

## 🎯 Klíčové rozhodnutí: DVĚ migrační strategie

U Supabase-native appek „Supabase" ≠ jen Postgres. Je to **Postgres + PostgREST
(`.from()` wire protokol) + GoTrue Auth (`auth.users`, `auth.uid()`) + Storage +
Realtime** (+ u agent-farm rozšíření **pgmq**). Proto existují dvě cesty:

### Strategie A — Self-hostovaný Supabase stack (NÍZKÉ RIZIKO) ⭐
Rozjedeme **celý Supabase stack u nás na Dokploy** (Postgres + GoTrue + PostgREST
+ Storage + Realtime + Studio — je to open source, běží v Dockeru). Appka pak jen
**přesměruje `SUPABASE_URL` + klíče** na náš server a nasměruje GoTrue SMTP na
LaunchMail. **RLS, Auth, Storage, Realtime fungují dál beze změny kódu.**
- ✅ Minimum přepisování, minimum rizika, data i chování zachovány
- ✅ Ideál pro **produkci s klienty (Freio)** a hluboce provázané appky
- ➖ Běží víc kontejnerů (víc RAM), pořád „Supabase" (ale tvůj, zdarma, lokální)
- 📄 Detail: `self-hosted-supabase.md`

### Strategie B — Holý Postgres + Better Auth + app-layer authz (ČISTÉ, ale práce)
Náš sdílený Postgres, **Supabase Auth → Better Auth**, **RLS → autorizace v kódu**,
`supabase-js` → `pg`/Drizzle, Storage → R2. „Nejčistší", ale velké přepisování a
vyšší riziko u appek, které na RLS spoléhají.
- ✅ Nejlehčí stack, plně náš, jeden sdílený Postgres
- ➖ Hodně přepisování; riziko „obnažení" dat, když se přehlédne authz filtr
- ✅ Vhodné pro appky s **connection-string DB** (ripieno, agent-farm backend,
   loot, leadcrm) a pro nové/malé projekty
- 📄 Detail: `supabase-auth-to-better-auth.md` + `supabase-postgres-data-migration.md`

**Doporučení:** hybridně podle projektu (tabulka níž). U Freio a ostatních
RLS-heavy produkcí jdi **Strategií A**. U connection-string appek Strategií B.
Časem lze přejít z A na B, až bude klid.

---

## 📋 Inventura projektů (13)

| Projekt | Co to je | DB dnes | Auth | E-mail | Storage | Hosting | Náročnost | Doporučená strategie |
|---------|----------|---------|------|--------|---------|---------|-----------|----------------------|
| **freio** ⭐ | SaaS SCIO testy, **produkce + klienti + hodně dat** | supabase-js **+ RLS** (raw SQL) | Supabase (email/pw + Google) | **Resend** (+Contacts/Audiences/PDF) | Supabase (`marketing-assets`) | Vercel + cron | 🔴 nejvyšší | **A** (self-host Supabase) → `migrate-freio.md` |
| **dentallocal** | Multi-tenant SaaS (6 apek) | supabase-js **+ RLS (~72)** | Supabase (magic+Google) | Resend SDK + react-email | Supabase (`reports`) | Vercel | 🔴 hard | **A** |
| **agent-farm** | Farma AI agentů | Drizzle+`DATABASE_URL` **+ pgmq/Realtime/Storage/Auth** | Supabase | — (Telegram) | Supabase (`media`) | Docker/Hetzner | 🔴 hard | **A** (Postgres s **pgmq**) |
| **explain-and-act** | Mobil + API (čtení dokumentů) | supabase-js + RLS | Supabase | GoTrue | Supabase (`documents`) | Vercel+EAS | 🔴 hard | **A** ⚠️ mobil sahá na DB přímo |
| **claude-trader** | Trading appka + worker | supabase-js + RLS + Realtime | Supabase (pw+magic+OAuth) | Resend (per-user klíče) | — | Vercel+Railway | 🟡 medium | A nebo B |
| **ripieno** | Agentní platforma (monorepo) | Drizzle+`SUPABASE_DB_URL` **+ pgvector/RLS/Realtime/Storage** | Supabase (multi-tenant) | — | Supabase (2 buckety) | Vercel | 🟠 med-high | **A** (kvůli pgvector+Realtime) |
| **life-admin-agent** | EU261 nároky z letů | **`pg` path už existuje** | Supabase (hardwired) | **nodemailer SMTP** už | Supabase (`documents`) | Vercel | 🟢 medium | **B** (DB+mail triviální; Auth+Storage dořešit) |
| **loot** | (SaaS) | Drizzle+pg, **půl-migrované** | Supabase → argon2 (probíhá) | Resend | ? | Vercel | 🟢 low | **B** (dokončit jeho `MIGRATION.md`) |
| **leadcrm** | CRM leadů | supabase-js service-role, **bez RLS** | **vlastní** (`app_users`) | **LaunchMail už** ✅ | Supabase (`leads`) | **Docker/Dokploy už** ✅ | 🟢 triviální | **B** (referenční vzor) |
| **hummy** | Mobil „Shazam broukání" | Supabase PG service-role **+ Deno Edge Fns** | žádný (device id) | — | — | EAS + supabase fns | 🟢 easy | **A** (edge runtime) nebo port Deno→Node |
| **nate_trader** | Trading bot | **žádná (JSON soubory)** | žádný | — (ClickUp) | — | scripts/cron | ⚪ nic | — |
| **openClawTrader** | Trading bot | **self-hosted PG už** (psycopg2) | žádný | — (Telegram) | — | Docker | ⚪ hotovo | jen `DATABASE_URL` na náš PG |
| **teriProjekt** | Flutter recepty | lokální SQLite (Drift) | žádný | — | lokální | Vercel (landing) | ⚪ N/A | — |

---

## 📚 Struktura dokumentů

**Cross-cutting (metodika, platí napříč):**
- `supabase-postgres-data-migration.md` — bezpečný přesun DAT bez ztráty (pg_dump/restore, ověření, cutover, rollback) — **páteřní**
- `self-hosted-supabase.md` — Strategie A: rozjetí Supabase stacku na Dokploy + „repoint"
- `supabase-auth-to-better-auth.md` — Strategie B: migrace authu (zachování UUID, bcrypt bez resetů, RLS→app)
- `email-to-launchmail.md` — náhrada Resend/nodemailer za LaunchMail (všude)
- `storage-to-r2.md` — Supabase Storage → R2 (nebo self-hosted Storage)
- `deploy-to-dokploy.md` — přesun hostingu z Vercelu/Railway na náš Dokploy

**Per-typ playbooky (stručné, odkazují na výše):**
- `type-nextjs-supabase-fullstack.md` — Supabase-native Next.js (claude-trader, dentallocal, explain-and-act, life-admin-agent)
- `type-nextjs-drizzle-postgres.md` — Drizzle/pg přes connection string (ripieno, agent-farm, loot)
- `type-mobile-expo.md` — Expo mobil (hummy, explain-and-act mobil) — API vrstva
- `type-python-and-static.md` — Python boti + statické (nate_trader, openClawTrader, teriProjekt)

**Deep runbook:**
- `migrate-freio.md` — kompletní pečlivý postup pro Freio (produkce, klienti, data)

---

## 🧭 Doporučené pořadí migrace (od nejsnazšího, ať se „vytrénujeme")

1. **openClawTrader / nate_trader / teriProjekt** — ⚪ skoro nic (repoint / N/A). Rozehřátí.
2. **leadcrm** — 🟢 už na Dokploy + LaunchMail; jen DB (bez RLS, vlastní auth). **Referenční vzor.**
3. **life-admin-agent** — 🟢 `pg` path + nodemailer už existují; dořešit Auth+Storage.
4. **loot** — 🟢 dokončit jeho vlastní rozpracovanou migraci (Strategie B).
5. **hummy** — edge functions rehost (Strategie A / port Deno).
6. **claude-trader** — 🟡 single-user Supabase.
7. **ripieno / agent-farm** — 🟠 Strategie A (pgvector / pgmq + Realtime).
8. **dentallocal** — 🔴 multi-tenant, 6 apek, Strategie A.
9. **freio** — 🔴 **poslední a nejpečlivěji** (produkce + klienti), viz `migrate-freio.md`.

> Freio dělej až budeš mít odladěný postup na ostatních. Nikdy ne jako první.

---

## 🔒 Bezpečnostní poznámka (udělej při migraci každého projektu)

Několik projektů má **živé secrets commitnuté v `.env` / `.env.local`** v gitu
(Freio: Stripe live + service-role JWT + Firebase/Google Play privKey + Anthropic
+ `ADMIN_PASSWORD`; nate_trader: Alpaca/Perplexity/ClickUp; agent-farm: model
klíče). Při migraci **VŠECHNY tyhle secrets rotuj** a nová hesla drž jen v Dokploy
env / našem `secrets/` (gitignored) — nikdy zpět do gitu.

**Nepřenositelné šifrovací klíče (bez nich jsou data ztracená — přenes je 1:1):**
`ENCRYPTION_KEY` (claude-trader per-user Resend), `RIPIENO_LOCAL_KEK` (ripieno
secrets), `AUTH_SECRET` (leadcrm), `CREDENTIALS_ENCRYPTION_KEY` (agent-farm),
`EMAIL_PREFERENCES_SECRET` (freio unsubscribe), `MAIL_ENCRYPTION_KEY` (launchmail).

---

## ✅ Univerzální „done" kritéria pro každý projekt

- [ ] Data ověřena (počty řádků zdroj == cíl; spot-check reálných záznamů)
- [ ] Auth funguje bez nucených resetů; doménová data neosiřela (UUID zachována)
- [ ] E-maily jdou přes LaunchMail a dorazí do inboxu
- [ ] Soubory (Storage) dostupné na nových URL
- [ ] Appka běží na Dokploy přes tunnel; klíčový flow projde smoke testem
- [ ] Projekt je v noční R2 záloze
- [ ] Supabase/Vercel/Resend necháno běžet jako fallback ≥ 1–2 týdny, pak teprve vypnout
