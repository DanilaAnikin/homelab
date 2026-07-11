# Strategie A: Self-hostovaný Supabase stack na Dokploy

Pro Supabase-native appky (Freio, dentallocal, agent-farm, explain-and-act,
ripieno, claude-trader, hummy) je „Supabase" hluboce provázaný: `.from()` přes
**PostgREST**, přihlašování přes **GoTrue** (`auth.users`, `auth.uid()` v RLS),
soubory přes **Storage**, živé updaty přes **Realtime**. Přepsat to všechno na
holý Postgres je riziko a spousta práce.

**Řešení:** Supabase je open source a běží v Dockeru. Rozjedeme **celý stack u nás**
a appka jen přesměruje URL + klíče. Kód, RLS, Auth, Storage, Realtime **zůstanou
beze změny**. Je to pořád „tvůj Supabase" — jen lokální, zdarma, na tvém železe.

---

## Co stack obsahuje

| Kontejner | K čemu | Nutné? |
|-----------|--------|--------|
| **postgres** | databáze (Supabase image s rozšířeními) | ✅ |
| **gotrue** (auth) | přihlašování, JWT, `auth.users`, OAuth, e-maily | ✅ (má-li appka Auth) |
| **postgrest** (rest) | `.from()` REST API nad Postgresem (respektuje RLS) | ✅ (má-li supabase-js `.from()`) |
| **storage-api** | soubory (buckety), signed URL | ⚠️ jen má-li appka Storage |
| **realtime** | `postgres_changes` websockety | ⚠️ jen má-li appka Realtime |
| **kong** (gateway) | jednotný vstup + směrování na služby, API klíče | ✅ |
| **studio** | admin UI (jako Supabase dashboard) | volitelné (za Tailscale) |
| **meta**, **imgproxy**, **vector/analytics** | pomocné | dle potřeby |

> Oficiální zdroj: [supabase/docker](https://github.com/supabase/supabase/tree/master/docker)
> (`docker-compose.yml` + `.env`). To je základ, který přizpůsobíme pro Dokploy + Tailscale/Tunnel.

## Nasazení na Dokploy

1. **Compose service** z `supabase/docker` (nebo náš fork v `compose/supabase/`).
2. **Vygeneruj vlastní secrets** (NE výchozí z ukázky!):
   - `POSTGRES_PASSWORD`, `JWT_SECRET` (min. 32 znaků),
   - z `JWT_SECRET` odvoď **`ANON_KEY`** a **`SERVICE_ROLE_KEY`** (Supabase má
     generátor; jsou to podepsané JWT s rolí `anon` / `service_role`),
   - `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` (Studio),
   - `SECRET_KEY_BASE`, `VAULT_ENC_KEY` (realtime/storage).
3. **SMTP pro GoTrue → LaunchMail:** nastav `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
   `SMTP_PASS`, `SMTP_ADMIN_EMAIL`, `SMTP_SENDER_NAME` na náš mail (Seznam smarthost
   nebo LaunchMail SMTP bránu) — tím jdou i **potvrzovací a reset-hesla maily** přes nás.
4. **Síť:** Kong gateway vystav přes Cloudflare Tunnel jako `supabase.<doména>` (API)
   a Studio jen přes **Tailscale** (nikdy veřejně). Interní služby drž v Docker síti.
5. **Sdílení jedné instance vs per-projekt:** můžeš mít **jednu Supabase instanci
   pro víc projektů** (každý projekt = vlastní schéma/DB + vlastní JWT projekt), nebo
   raději **oddělené instance na projekt** kvůli izolaci auth uživatelů a klíčů.
   Pro produkci (Freio) doporučuju **vlastní izolovanou instanci**.

## Migrace dat + schématu do self-hosted Supabase

Protože cíl je taky Supabase (Postgres + `auth`/`storage` schémata), je to
**nejvěrnější možná migrace** — dumpneš celé relevantní schéma včetně `auth` a
`storage` a restoreneš:

```bash
# na zdrojovém (cloud) Supabase — plný dump aplikačních + auth + storage schémat:
pg_dump "$SUPABASE_CLOUD_URL" --format=custom --no-owner --no-privileges \
  --schema=public --schema=auth --schema=storage -f app_full.dump

# do našeho self-hosted Postgresu (uvnitř stacku):
pg_restore --no-owner --no-privileges --schema=public --schema=auth --schema=storage \
  -d "$SELF_HOSTED_PG_URL" --jobs=4 app_full.dump
```
- **`auth.users` se přenesou 1:1** → uživatelé i hesla (bcrypt) fungují dál, žádné resety.
- **RLS policies fungují**, protože `auth.uid()` opět existuje (GoTrue běží).
- Ověření integrity: viz `supabase-postgres-data-migration.md` krok 6.

## Přepnutí aplikace (repoint) — minimum kódu

V env appky jen přepiš:
```env
NEXT_PUBLIC_SUPABASE_URL=https://supabase.tvojedomena.cz     # náš Kong gateway
NEXT_PUBLIC_SUPABASE_ANON_KEY=<náš anon JWT>
SUPABASE_SERVICE_ROLE_KEY=<náš service_role JWT>
# u appek s přímým DB stringem (ripieno SUPABASE_DB_URL, agent-farm DATABASE_URL):
SUPABASE_DB_URL=postgres://...@supabase-db:5432/postgres
```
Kód `supabase-js` (`.from()`, `.auth`, `.storage`, `.rpc()`, Realtime) zůstává.
`next.config` `images.remotePatterns` a CSP přepiš z `*.supabase.co` na naši doménu.

## Rozšíření, na která pozor (musí být v našem Postgres image)

- **pgvector** (ripieno `document_embeddings`, `match_documents`) → Supabase image ho má.
- **pgmq** (agent-farm dispatch fronta) → **není** v základním Supabase image!
  Musíš použít Postgres image s pgmq (Supabase ho v novějších verzích přibaluje, jinak
  doinstaluj) — jinak `CREATE EXTENSION pgmq` spadne a agent-farm nejede.
- **pg_trgm** (agent-farm dedup) → standardní, OK.
- `uuid-ossp`, `pgcrypto` → v Supabase image jsou.

## Storage soubory

Metadata (`storage.objects`) se přenesou dumpem, ale **samotné soubory** v bucketech
musíš zkopírovat zvlášť do našeho Storage (nebo do R2, viz `storage-to-r2.md`).
Self-hosted `storage-api` umí jako backend souborový systém (volume) nebo S3/R2.

## Realtime

Self-hosted `realtime` kontejner obslouží `postgres_changes` stejně jako cloud.
Appka nemění nic (`.channel().on('postgres_changes', ...)` funguje). Jen musí být
publikace `supabase_realtime` v DB (přenese se dumpem) a kontejner běžet.

## Výhody / nevýhody Strategie A

✅ Minimum přepisování, data + chování 1:1, žádné resety hesel, RLS/Storage/Realtime
funguje, ideální pro produkci s klienty.
➖ Víc kontejnerů (RAM ~1–2 GB navíc na instanci), správa Supabase stacku, updaty.
➖ Pořád „Supabase" (ale lokální/zdarma) — kdo chce úplně pryč, migruje časem na
Strategii B, až bude klid a odladěno.

## Kdy raději Strategii B

Když appka **nepoužívá RLS/Auth/Realtime/Storage přes supabase-js** a jen má DB přes
connection string (ripieno DB-path, agent-farm backend, loot, leadcrm) — tam je
holý Postgres + Better Auth čistší a lehčí. U appek, které na Supabase službách
visí (Freio, dentallocal, explain-and-act, hummy edge fns), je A výrazně bezpečnější.
