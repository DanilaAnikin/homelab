<!-- Ripieno Strategie B (Better Auth + Drizzle + shared-postgres + SSE + R2). Workflow 2026-07-26. Návrh, PROVÁDÍ SE po fázích. -->

# Plán migrace Ripieno na Strategii B (Better Auth + Drizzle + shared-postgres + SSE + R2)

## STAV (aktualizováno 2026-07-26)

- ✅ **Fáze 0–3 HOTOVO**: infra, schéma (33 tabulek + funkce aplikované v DB `ripieno`), sdílené balíčky (events/kb) + celý engine na Drizzle.
- ✅ **Fáze 4–7 HOTOVO** (commit `cb0b3b9` na větvi `feat/homelab-migration`): Better Auth (magic-link přes LaunchMail, provisionUser), všechny web selecty/zápisy na Drizzle scopované na org, realtime→polling (notif 20 s, run 4 s), storage→R2 (`lib/r2.ts`). Supabase kompletně vytržen (smazané `lib/supabase/*`, `@ripieno/db` client, kb supabase-store; odebrané `@supabase/*` deps). **Celé monorepo typecheckuje, biome čistý.**
- ✅ **Fáze 9 PŘIPRAVENO** (commit `8087d48`): Dockerfily web+engine, next.config standalone, .dockerignore, turbo.json globalEnv.
- ✅ **Fáze 8 SKRIPT PŘIPRAVEN**: `ripieno-data-migration.sh` (spustí se při cutoveru).
- ⏳ **BLOKÁTORY (přímý zásah uživatele)**: (1) schránka `contact@ripieno.xyz` na Seznamu → LaunchMail smtp config; (2) R2 bucket `ripieno-uploads` + scoped token. Pak Fáze 9 deploy (Dokploy env + 2 appky) → Fáze 10 cutover (data migrace + DNS flip + Stripe/GitHub ověření).

## 1. Shrnutí rozsahu

- **~118 touch-pointů se Supabase**: 102 PostgREST `.from()` (29 souborů), 4 RPC, 4 storage volání, 3 realtime subscriptions (4 kanály), 5 auth call-sitů.
- **~65 souborů k úpravě** napříč `apps/web`, `apps/engine`, `packages/{db,events,kb,core}`.
- **Výhoda startu**: Drizzle infra už z ~60 % existuje — `packages/db/src/client.ts` má `createDb/getDb`, `schema.ts` má 26 z 29 tabulek. Chybí jen `run_seq`, `stripe_processed_events` a 4 Better Auth tabulky.
- **Vzor k okopírování**: `/home/anakin/programming/dentallocal` (Lokwave) — tuto migraci už dokončil (auth context, drizzle Pool, R2 adapter). Realtime/SSE je jediná část BEZ vzoru (Lokwave ho nemá).
- **Hrubý effort**: **L, ~5–8 dní** fokusovaně. App-layer vytržení (auth+data+realtime+storage) je L (~3–5 dní) a **blokuje** deploy-cutover (M, ~1 den).

## 2. Doporučená datová strategie

**DOPORUČENÍ: Plný data-only restore se zachováním UUID** (ne semi-fresh).

Zdůvodnění:
- Tvar business tabulek pod B je **identický** s dnešním public schématem (`schema.ts` je jejich zrcadlo) → přenos = čistý 1:1 COPY, žádná transformace kromě auth.
- Objem triviální: 1 user, 1 org, 14 runs, 98 docs, 3312 events → restore ~15 min.
- Zachování UUID = **nula re-pointingu** přes ~25 sloupců `user_id`/`organization_id`. FK řetězce (`documents.run_id` + `organization_id` NOT NULL, `document_embeddings.document_id` → documents) drží.
- Semi-fresh je **paradoxně víc práce** — kvůli NOT NULL FK bys stejně musel přenést org+runs+documents+embeddings a re-pointnout je na nové ID.

**Auth uživatele (founder)**: neseedovat přes Better Auth sign-up (spustil by hook → duplicitní org/wallet). Místo toho **přímý SQL INSERT** do `public."user"` se **stejným UUID** jako dnešní `auth.users.id`. Heslo nepřenášet (bcrypt ≠ scrypt) → founder se přihlásí **magic-linkem přes LaunchMail** (už běží na mail.ripieno.xyz).

**Zdroj (hostovaná Supabase) NEMAZAT** — držet warm jako fallback min. 1–2 týdny.

## 3. Fáze refaktoru (v proveditelném pořadí)

### Fáze 0 — Prerekvizity infra (BLOKER, mimo repo)
- **pgvector do shared-postgres**: dnes ho image NEMÁ (`homelab/docs/migrations/migrate-freio.md:39`). Přebuildit na `pgvector/pgvector:pg17`, restart. Bez toho restore embeddings spadne „type vector does not exist".
- Vytvořit DB + roli: `/srv/homelab/scripts/newdb.sh ripieno`, pak `CREATE EXTENSION vector; CREATE EXTENSION pgcrypto;`.
- `/srv/homelab/secrets/ripieno.env` s `DATABASE_URL` (pooled :6432), `DIRECT_URL` (:5432), `BETTER_AUTH_SECRET`, `R2_*`, `RIPIENO_LOCAL_KEK` (**přenést doslova, NEROTOVAT**), `ENGINE_SHARED_SECRET`.
- **Ověření**: `psql -d ripieno -c '\dx'` ukáže vector; `\du` ukáže roli ripieno.
- **Rollback**: triviální, nic produkčního se nedotýká.

### Fáze 1 — DB klient + schema (packages/db)
- `client.ts`: `SUPABASE_DB_URL` → `DATABASE_URL`, **node-postgres `pg.Pool` s `keepAlive:true`** (vzor `dentallocal/packages/core/src/server/db/drizzle.ts`). Lokwave varuje: postgres-js pooled klient na Docker overlay síti tiše umře (reálný bug z LaunchMailu). Smazat `createServiceClient` + `@supabase/supabase-js`.
- `schema.ts`: doplnit `run_seq`, `stripe_processed_events`, 4 Better Auth tabulky (`user/session/account/verification`, kopie `dentallocal/.../schema/auth.ts`). Přidat chybějící indexy (memberships unique, events seq indexy, connector_accounts partial unique, `document_embeddings` hnsw `vector_cosine_ops`). `.$onUpdate()` na 19 tabulek s `updated_at` (nahradí trigger).
- `drizzle.config.ts`: `url=DIRECT_URL`, `casing:'snake_case'`. `migrate.ts` → `DIRECT_URL`.
- Vygenerovat jednu strategy-B migraci **BEZ RLS/triggerů/realtime publikací**. Follow-up SQL: hnsw index, `match_documents()`, `next_event_seq()`, `increment_topup_balance()` (bez `auth.uid()`, bez SECURITY DEFINER nutnosti).
- **Ověření**: `pnpm --filter @ripieno/db migrate` proti prázdné DB projde; `\dt` = 33 tabulek. `pnpm -r typecheck` v packages/db čistý.
- **Rollback**: `DROP DATABASE ripieno`, znovu.

### Fáze 2 — Repo/query vrstva + tenant kontext (packages/db)
- `packages/db/src/server/context.ts` (kopie `dentallocal/.../auth/context.ts`): `requireOrgContext/getOrgContext`, `assertAdmin`, `assertOwnedByOrg`. **Nahrazuje RLS** (mig 0001).
- `packages/db/src/repo/*.ts` (~40–50 funkcí, každá bere `db` + explicitní `organizationId`/`runId`): `orgs, runs, agents, events, messages, notifications, billing, connectors, github, kb, anthropic-keys`.
- 3 RPC → Drizzle (zachovat **atomicitu**, ne read-modify-write):
  - `next_event_seq` → `onConflictDoUpdate value = run_seq.value+1 returning`.
  - `increment_topup_balance` → `onConflictDoUpdate topupBalance = credit_wallet.topup_balance + excluded.topup_balance`.
  - `match_documents` → raw `sql\`... embedding <=> ${vec} ...\``. Přejmenovat `kb/supabase-store.ts` → `drizzle-store.ts`.
- **Ověření**: unit test každé repo funkce proti lokální DB; ověřit že seq je monotónní pod paralelním voláním.

### Fáze 3 — Engine na Drizzle (apps/engine + packages/events)
- Mechanický přepis (SupabaseClient → Drizzle db/repo): `run-store.ts` (21 call-sitů — jádro), `runner.ts`, `wallet.ts` (pozor race na saveWallet — použít SQL onConflict), `sweep.ts`, `orchestrate.ts`, `connectors.ts`.
- `packages/events/{writer,reader,seq}.ts` na Drizzle. `env.ts`: `hasSupabaseEnv` → `hasDbEnv`.
- Přepsat mocky v `battery.ts`, `smoke.ts`, `*.test.ts` (jinak spadne CI).
- **Ověření**: `pnpm --filter @ripieno/engine test`; ruční `/trigger` smoke run zapíše events/agents do nové DB.
- **Rollback**: engine je stateless proti DB, přepnout env zpět.

### Fáze 4 — Auth: Better Auth (apps/web)
- `lib/auth/better-auth.ts`: `betterAuth` + `drizzleAdapter(pg)` + `generateId=randomUUID`. **Rozhodnutí A vs B viz sekce 5.**
- **KLÍČOVÝ `databaseHook` user.create.after** — musí v transakci replikovat CELÝ `handle_new_user`: insert `users` → `organizations` → `memberships(owner)` → `credit_wallet(balance=200)` → set `default_organization_id`. Idempotentně (guard „už má membership?"). Lokwave hook dělá jen mirror — **tady rozšířit**, jinak nové signupy nedostanou org+kredity a `wallet.ts` gate odmítne runy.
- `app/api/auth/[...all]/route.ts` = `toNextJsHandler(auth)`. `lib/auth/auth-client.ts` = `createAuthClient`.
- Přepsat `lib/auth.ts` (zachovat signatury `getUser/requireUser/getActiveOrg` → 13 call-sitů se nemění): session přes `auth.api.getSession`, org přes Drizzle join memberships.
- `login/actions.ts` (signIn/signUp/signOut přes authClient), `middleware.ts` (`getSessionCookie` místo `updateSession`, **zachovat www→apex 308 redirect**).
- **SMAZAT**: `lib/supabase/{client,server,service,env,middleware}.ts`, `auth/callback/route.ts`. `package.json`: −`@supabase/ssr`, +`better-auth`.
- **Ověření**: magic-link login foundera → session → dashboard čte přes Drizzle. `pnpm --filter @ripieno/web build` čistý.

### Fáze 5 — Tenant filtr do web selectů (apps/web)
- Každý dnešní ANON-server select dostane explicitní `.where(eq(table.organizationId, ctx.organizationId))`: `lib/{models,run-view,github-connection}.ts`, `dashboard/runs/projects/billing page.tsx` + všechny `actions.ts`.
- service_role akce (`billing-applier`, `admin`, `settings`) → repo funkce.
- **2 browser přímé čtení → server actions**: `notification-bell.tsx` (`getNotifications`, `markNotificationRead`), `run-workspace.tsx` (messages přes initial props).
- **Ověření**: **audit každého call-situ** na přítomnost org filtru (cross-tenant leak checklist). Manuální test: přístup k cizímu runId/orgId vrátí prázdno/403.

### Fáze 6 — Realtime → SSE (apps/web + engine)
- MVP: **polling** `GET /api/runs/[runId]/events?since=<seq>` každé 1–2s (pro 1 usera dostačuje) — pošli jako první krok.
- Enhancement: SSE `app/api/runs/[runId]/stream/route.ts` (`runtime='nodejs'`), napájený **Postgres LISTEN/NOTIFY** přes postgres.js (`sql.listen`). **Sdílený modulový listener** (`lib/realtime/listener.ts`, 1× listen + in-process fan-out podle runId) — jinak každý tab vyčerpá pool. Emit `notify()` v writer/run-store/actions po insertu (jen ukazatel kind+seq, kvůli 8000B limitu). Heartbeat `: ping` každých ~25s (Cloudflare Tunnel idle timeout). Podpora `Last-Event-ID` + backfill `seq>last`.
- Klient: `run-workspace.tsx`/`notification-bell.tsx` → `EventSource`, reducery `mergeEvent/mergeMessages` zůstávají 1:1.
- **Ověření**: run stream se živě aktualizuje; reconnect nevynechá event; tenant scoping ověřen (`run.organization_id == ctx.organizationId`).

### Fáze 7 — Storage → R2 (packages/core + engine + web)
- `packages/core/src/storage/r2.ts` (copy `dentallocal/.../adapters/r2/client.ts`): `putObject/getSignedReadUrl/deleteObject`, `@aws-sdk/client-s3` + presigner. Jeden bucket `ripieno-uploads` s prefixy `discovery/` + `raw-logs/`, **PRIVATE**, vlastní scoped R2 token.
- `attachment-actions.ts`: přepis na R2, key prefix `discovery/${org.id}/`, guard sedí na nový layout.
- **Dekaplovat EventWriter** (`writer.ts`): přidat injektovaný `uploadRawLog?`, odpojit od Supabase clientu. Wiring v `runner.ts`/`sweep.ts` přes `putObject`.
- **Ověření**: upload přílohy → signed URL náhled funguje; raw log se zapíše do R2 (`raw_log_ref` se nikde nečte → formát klíče kosmetický).

### Fáze 8 — Data migrace (skripty)
- `scripts/restore-data.sh`: `pg_restore --data-only --schema=public --no-owner --role=ripieno` + `SET session_replication_role=replica` (obejít FK pořadí). Auth schema NErestorovat.
- `scripts/seed-auth-user.sql`: 1 řádek `auth.users` → `public."user"` (stejné id, email_verified=true). Přímý SQL, ne sign-up.
- `scripts/verify-integrity.sh`: row-count zdroj vs cíl (org=1, runs=14, docs=98, events=3312), FK sirotci=0, `vector_dims<>1024`=0, wallet balance sedí, `match_documents` smoke, `max(seq)` per run sedí.
- Storage: zkopírovat `discovery-uploads` + `raw-logs` do R2 (nebo akceptovat ztrátu — pre-launch). Grep `supabase.co/storage` v datech kvůli path refům.
- **Rollback**: cíl je nová DB, zdroj netknutý.

### Fáze 9 — Docker + Dokploy (deploy-cutover)
- `apps/web/next.config.ts`: `output:'standalone'` + `outputFileTracingRoot` na monorepo root.
- `apps/web/Dockerfile` (kopie `dentallocal/Dockerfile`, multistage, **`ENV HOSTNAME=0.0.0.0`** — jinak 502, poučení LaunchMail), `NEXT_PUBLIC_APP_URL=https://www.ripieno.xyz` jako build ARG.
- `apps/engine/Dockerfile` (node:22-slim, pnpm, `tsx start`, :8787, HEALTHCHECK /health, **BEZ public domény**). Musí dostat `ENGINE_SHARED_SECRET` jinak exit 1.
- `.dockerignore`. `turbo.json` globalEnv: SUPABASE_* → DATABASE_URL/DIRECT_URL/BETTER_AUTH_SECRET/R2_*.
- Dokploy 2 appky (`ripieno-engine` nejdřív, ověřit /health interně; pak `ripieno-web`, `ENGINE_URL=http://ripieno-engine:8787`).
- **Ověření**: staging hostname `ripieno-staging.ripieno.xyz` přes tunnel — plný flow (login, run, stream, upload, billing).

### Fáze 10 — Cutover DNS + Stripe/GitHub (atomický)
- Doména `www.ripieno.xyz` **zůstává** → cutover = přepnutí Cloudflare DNS/tunnel z Vercelu na `ripieno-web:3000` (apex i www → ripieno-web, middleware redirectuje apex→www). URL webhooků se NEMĚNÍ.
- Ověřit `STRIPE_WEBHOOK_SECRET` odpovídá stávajícímu endpointu (jinak aktualizovat). Test Stripe event + GitHub App flow na nové origin.
- **Ověření**: monitoring Grafana/Loki/Kuma, Stripe test event projde, magic-link login OK.
- **Rollback**: flip DNS zpět na Vercel (instant). Railway engine + Supabase držet **warm 7 dní**.

### Fáze 11 — Decommission
- Po 7 dnech bezchybného provozu: vypnout Vercel + Railway, **pause** (ne delete) Supabase.

## 4. Rizika a zmírnění

| Riziko | Zmírnění |
|---|---|
| **Ztráta RLS → cross-tenant leak** (~30 ANON selectů) | Centralizovat přes `requireOrgContext`, nikdy nevolat db bez `organizationId` u tenant tabulek. Audit každého call-situ (Fáze 5). Dopad teď malý (1 user), ale MUSÍ být hotové před dalšími uživateli. |
| **databaseHook musí replikovat celý `handle_new_user`** | Rozšířit Lokwave mirror hook o org+membership+wallet(200)+default_org, idempotentně. Jinak signup bez kreditů → wallet gate odmítne runy. |
| **Migrace hesla foundera** (bcrypt ≠ scrypt) | Nepřenášet. Seed jen řádek `user` se stejným UUID, login magic-linkem (LaunchMail). Fresh signup by dal nové id → nutný re-point 14 runs/98 docs. |
| **RIPIENO_LOCAL_KEK** | Přenést **doslova, NEROTOVAT** — jinak nedešifrovatelné connector/anthropic secrets. |
| **Atomicita seq/wallet** | Zachovat SQL `onConflict` (ne select-then-update) u `next_event_seq`, `increment_topup_balance`, `saveWallet`, Stripe idempotence (`stripe_processed_events` unique). |
| **postgres-js tichá smrt na Docker Swarm** | node-postgres `pg.Pool` + `keepAlive:true`. |
| **Realtime parita** | Polling MVP pro 1 usera OK; SSE (LISTEN/NOTIFY + sdílený listener + heartbeat + Last-Event-ID) přidat před reálným provozem — živý stream eventů je jádro produktu. |
| **pgvector chybí v shared-postgres** | Fáze 0 blocker — rebuild image `pgvector/pgvector:pg17` PŘED migrací. |
| **Next standalone bind** | `HOSTNAME=0.0.0.0` v runner stage. |
| **Cutover** | Doména zůstává → atomický DNS flip, staging test předem, Supabase/Vercel/Railway warm 7 dní pro instant rollback. |

## 5. Rozhodnutí pro uživatele

1. **Auth flow — email+heslo vs magic-link?**
   Ripieno má dnes taby heslo. **DOPORUČENÍ: magic-link** (varianta B) — LaunchMail už běží, nulová migrace hesla, jednodušší. Zachovat email+heslo (varianta A) jen pokud výslovně chceš UX beze změny (pak custom scrypt seed nebo reset hesla foundera). Volitelně přidat Google social u obou.

2. **Data: plný restore vs semi-fresh?**
   **DOPORUČENÍ: plný restore se zachováním UUID** (viz sekce 2). Semi-fresh volit jen pokud vědomě zahazuješ 14 runs / 3312 events — je to víc práce za méně dat.

3. **Realtime: polling MVP nebo rovnou SSE?**
   **DOPORUČENÍ: polling jako první krok** (cutover-ready pro 1 usera), SSE doplnit hned poté. Živý event stream je jádro, takže SSE nesmí zůstat dlouho odloženo.

4. **Storage stará data: migrovat nebo fresh?**
   **DOPORUČENÍ: fresh** — přílohy jsou efemérní, raw-logs write-only (nikde se nečte), pre-launch. Potvrdit, že ztráta starých logů je OK.

5. **Pořadí bezpečnosti**: staré (Supabase/Vercel/Railway) necháváme běžet, dokud homelab neověříme (staging + 7 dní warm), zálohy máme. Žádné mazání zdroje před ověřením.

**Kritická cesta**: Fáze 0 → 1 → 2 jsou nutné před vším ostatním. Fáze 4 (auth context) musí přistát před/souběžně s Fází 5 (bez `requireOrgContext` nelze nahradit RLS scoping). Deploy (9–10) je blokován kompletním app-layer vytržením (2–8).