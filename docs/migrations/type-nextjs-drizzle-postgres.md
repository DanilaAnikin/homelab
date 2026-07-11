# Typ: Next.js/Node + ORM přes connection string (Drizzle/pg)

**Projekty:** ripieno, agent-farm, loot, leadcrm.

Společný znak: primární data jdou přes **ORM (Drizzle) nebo `pg` proti connection
stringu** — takže **DB se dá přesměrovat jednou proměnnou**. Háček je, že některé
z nich navíc používají Supabase i pro Auth/Storage/Realtime/rozšíření, a to je
skutečná práce.

> **DB migrace je u tohoto typu triviální** (změna connection stringu + spuštění
> jejich migrací proti našemu Postgresu). Rozhoduje, co „navíc" na Supabase visí.

---

## Základní postup (DB repoint)

1. `newdb.sh <projekt>` → nová DB + user na našem Postgresu.
2. Nainstaluj potřebná **rozšíření** do té DB (viz per-projekt níž).
3. Přenes data: `supabase-postgres-data-migration.md`.
4. Přepni connection string env → náš Postgres (pooled 6432 / direct 5432).
5. Spusť migrace projektu proti `DIRECT_URL`.
6. Vyřeš „navíc" věci (Auth/Storage/Realtime/pgvector/pgmq) — per-projekt.
7. E-mail → LaunchMail. Deploy na Dokploy.

## Per-projekt specifika

### leadcrm (🟢 triviální — REFERENČNÍ VZOR)
- **Už běží na Dokploy** a **už posílá přes LaunchMail** (`src/lib/launchmail.ts`
  je kanonický klient — zkopíruj ho do ostatních projektů). E-mail = hotovo.
- Auth je **vlastní** (`app_users`, PBKDF2 + HMAC cookie) — **žádný Supabase Auth,
  žádné RLS.** `supabase-js` je jen service-role PostgREST vrstva.
- DB: buď přepiš `.from()/.rpc()` builder na `pg`/Drizzle proti našemu Postgresu,
  nebo (rychlejší) drž `supabase-js` proti self-hosted Supabase. `.rpc()` funkce
  (`crm_stats`, …) jsou v migracích → portovatelné.
- Storage: bucket `leads` (CSV/screenshoty/HTML) → R2; `publicUrl` je čistý string
  builder (snadný repoint). **Pozor:** sdílí Supabase projekt s „leadgen" pipeline —
  export nesmí zahodit řádky, co píše generátor.
- `AUTH_SECRET` přenes 1:1 (HMAC session cookie).

### ripieno (🟠 med-high)
- **Hybrid:** primární data přes **Drizzle nad `SUPABASE_DB_URL`** (snadný repoint),
  ALE `supabase-js` pro **Auth (multi-tenant: orgs/memberships/teams/invites), RLS,
  Realtime (events/messages/notifications), Storage (2 buckety) a pgvector**.
- **pgvector:** `document_embeddings vector(1024)` + HNSW index + RPC `match_documents`
  → náš Postgres MUSÍ mít `CREATE EXTENSION vector`. `.rpc('match_documents')` přes
  PostgREST → při Strategii B volej přes SQL/Drizzle.
- Doporučení: **Strategie A** (Realtime + pgvector + multi-tenant auth pohromadě).
  Při B: Better Auth **organization plugin** + Realtime náhrada + Storage→R2.
- Bez e-mailu (notifikace jen in-app). `RIPIENO_LOCAL_KEK` přenes 1:1 (secrets tabulka).

### agent-farm (🔴 hard kvůli rozšířením)
- Backend: **Drizzle + `DATABASE_URL`** (service-role level, snadný repoint —
  vrať `PG_PREPARE=true` a zahoď pooler workaround).
- **Blokery, co holý PG nedá:**
  - **pgmq** (Postgres fronta = páteř dispatche úloh) → náš Postgres image MUSÍ mít
    pgmq, jinak `CREATE EXTENSION pgmq` spadne a farma nejede. **Nejvyšší riziko.**
  - **pg_trgm** (dedup) → standardní, OK.
  - **Supabase Auth** (dashboard, `auth.users` FK, RLS `auth.uid()` + `is_admin()`).
  - **Realtime** (dashboard live refresh) → bez toho manuální refresh.
  - **Storage** `media` bucket (MinIO adapter je stub, nedopsaný) → R2 nebo dopsat.
- Doporučení: **Strategie A s pgmq-enabled Postgresem.** E-mail: app-side žádný
  (lidem jde Telegram); GoTrue SMTP → náš mail. `CREDENTIALS_ENCRYPTION_KEY` přenes 1:1.
- Deploy: už má `docker-compose.farm.yml` (Hetzner) — přenes na Dokploy/náš server;
  Postgres přidej do stacku (dnes externí Supabase).

### loot (🟢 low — dokonči jeho vlastní migraci)
- **Už si migraci rozjel sám** (`MIGRATION.md`): Supabase → self-hosted PG na Dokploy,
  vlastní argon2 + `sessions` (Better-Auth-style), `src/lib/auth.ts` už nevolá Supabase.
- Nejlepší cesta: **dokončit dle jeho MIGRATION.md** (Strategie B) — případně jeho
  bespoke `sessions` nahradit Better Authem. **Zachovej UUID** (`brands.owner_id` …).
  Staré účty: bcrypt hashe ze Supabase `auth.users` → Better Auth account, ať se
  nemusí resetovat. E-mail (Resend) → LaunchMail.

## Univerzální ověření (tento typ)
- [ ] Rozšíření nainstalována (`vector`/`pgmq`/`pg_trgm`) — jinak app spadne při startu
- [ ] Migrace projektu proběhly proti našemu PG; počty řádků sedí
- [ ] Auth/Storage/Realtime „navíc" vyřešeno dle strategie
- [ ] Šifrovací klíče přeneseny (`AUTH_SECRET`/`RIPIENO_LOCAL_KEK`/`CREDENTIALS_ENCRYPTION_KEY`)
