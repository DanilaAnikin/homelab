# Self-hosted Supabase stack (Strategie A) — pro Dokploy

Rozjede **celý Supabase stack u nás** (Postgres + GoTrue Auth + PostgREST +
Storage + Realtime + Studio), aby Supabase-native appky (Freio, dentallocal,
agent-farm, explain-and-act, ripieno, hummy) jen **přesměrovaly `SUPABASE_URL` +
klíče** a jely dál — RLS, Auth, Storage, Realtime beze změny kódu.

> Kontext: `docs/migrations/self-hosted-supabase.md` (proč a kdy A vs B). Tenhle
> adresář je **připravená kostra** — v den migrace jen spustíš.
>
> Ověřeno proti `supabase/supabase@master` (2026-07). Image tagy i env se hýbou —
> před ostrým během **pinni upstream na konkrétní commit** a znovu zkontroluj.

## Co je uvnitř

| Soubor | K čemu |
|---|---|
| `fetch-upstream.sh` | stáhne oficiální `docker/` z upstreamu do `supabase-docker/` (pinovaně) |
| `.env.example` | kompletní env s placeholdery + příkazy na generování klíčů + „CHANGE" vlajky |
| `docker-compose.override.yml` | naše doplňky (Storage → R2) navrch upstream compose |
| *(gitignored)* `supabase-docker/`, `.env` | stažený upstream a tvoje ostrá env — nejdou do gitu |

## Proč nefetchujeme ručně

Upstream compose závisí na `volumes/` init skriptech, které **vytvářejí Supabase
role** (`anon`, `authenticated`, `service_role`, `supabase_admin`, …) a Kong
konfiguraci. Ruční kopie driftuje a rozbije Auth/Storage. Proto vendorujeme
skutečný upstream na pinovaném commitu a jen na něj vrstvíme `.env` + override.

---

## Postup nasazení

### 1) Stáhni upstream
```bash
cd compose/supabase
# ideálně nejdřív najdi konkrétní commit a: SUPABASE_REF=<sha> ./fetch-upstream.sh
./fetch-upstream.sh
```

### 2) Hardening úpravy staženého base (krátké, jednorázové)
V `supabase-docker/docker-compose.yml`:
- **Odeber službu `supavisor`** — pooler nepoužíváme a navíc **publikuje host
  porty `5432`/`6543`** (viz Bezpečnost). Appky jedou přes Kong, ne přes pooler.
- **Ověř, že NENÍ služba `analytics`/`vector`** (master ji v base nemá — je v
  `docker-compose.logs.yml`, který nepřidáváme).
- **Ověř, že `db` nepublikuje host port** (nemá — nech to tak).
- Nech jen: `db, kong, auth, rest, realtime, storage, imgproxy, meta, studio`
  (+ `functions` jen když ho projekt potřebuje, např. hummy edge funkce).

### 3) Env
```bash
cp .env.example .env
# vygeneruj klíče (příkazy jsou v .env.example jako komentáře):
openssl rand -hex 32   # POSTGRES_PASSWORD, JWT_SECRET, SECRET_KEY_BASE, VAULT_ENC_KEY, PG_META_CRYPTO_KEY
openssl rand -hex 8    # REALTIME_DB_ENC_KEY (přesně 16 znaků!)
openssl rand -base64 24 # DASHBOARD_PASSWORD
# ANON_KEY + SERVICE_ROLE_KEY = JWT podepsané JWT_SECRETem (viz .env.example → Node/Python one-liner)
chmod 600 .env
```
⚠️ **Přepiš KAŽDÝ řádek s „CHANGE — INSECURE DEFAULT".** Demo klíče jsou veřejné
a podepsané demo secretem → kdokoli by si zfalšoval `service_role` token a obešel
RLS. `API_EXTERNAL_URL` **musí obsahovat `/auth/v1`** (bez toho se rozbije OAuth
callback). `ENABLE_PHONE_SIGNUP`/`AUTOCONFIRM` upstream default je `true` → dej `false`.

### 4) Spusť
```bash
docker compose -f supabase-docker/docker-compose.yml -f docker-compose.override.yml up -d
```

### 5) Síť — Cloudflare Tunnel + Studio
- Nasměruj tunnel: **`supabase.<doména>` → `http://kong:8000`** (jen Kong!). TLS
  terminuje Cloudflare; Kong může za tunelem zůstat HTTP.
- Appka pak nastaví `SUPABASE_URL=https://supabase.<doména>` + anon key (klient)
  / service-role key (server).
- **Studio NENÍ automaticky privátní** — jede přes Kong na `/` (+ pg-meta na
  `/pg/*`), a `:8000` je přesně port, který tuneluješ. Takže dashboard by byl
  **veřejně dostupný** jen za basic-auth. **Zablokuj `/` a `/pg/*` na CF hraně**
  (Cloudflare Access se SSO / allow-listem, nebo tunnel/WAF pravidlo, které
  veřejně pustí jen `/auth/v1/*`, `/rest/v1/*`, `/storage/v1/*`, `/realtime/v1/*`,
  `/functions/v1/*` a zahodí `/` + `/pg/*`). Ke Studiu pak přes Tailscale/loopback
  na Kong. `DASHBOARD_*` basic-auth nech jako druhou vrstvu. V Dokploy **nepřipojuj
  Studiu samostatnou veřejnou doménu.**

---

## Storage → Cloudflare R2

`.env` má `STORAGE_BACKEND=s3` + `STORAGE_S3_*`; override je namapuje na Storage
službu (`GLOBAL_S3_*`/`AWS_*`). Napřed v Cloudflare založ R2 bucket + R2 API token
(read/write). `GLOBAL_S3_FORCE_PATH_STYLE=true` a endpoint jsou nutné pro R2
(non-AWS). `TUS_ALLOW_S3_TAGS=false` (R2 nemá x-amz-tagging → jinak resumable
uploady 500). `SignatureDoesNotMatch` = špatný `REGION`/klíče. Nechceš R2? Dej
`STORAGE_BACKEND=file` a soubory jsou na volume. **Nepoužívej `docker-compose.s3.yml`**
(spustí lokální MinIO, co nechceme). Přenos existujících souborů: `docs/migrations/storage-to-r2.md`.

## Rozšíření: pgvector + pgmq

`supabase/postgres` image má obojí k dispozici (per-DB `create extension`):
- **pgvector** (ripieno): `create extension if not exists vector;` — funguje, bez zásahu.
- **pgmq** (agent-farm): ⚠️ **na tagu `17.6.1.136` má známý bug** — Supabase
  `after-create.sql` dělá nejednoznačný `alter extension pgmq drop function
  pgmq.drop_queue;` a `create extension pgmq` může spadnout:
  `ERROR: function name "pgmq.drop_queue" is not unique (SQLSTATE 42725)`
  (tracking: supabase/supabase#39865). **Nespoléhej, že projde — ověř na přesném
  pinovaném image:**
  ```sql
  select name, installed_version from pg_available_extensions
   where name in ('vector','pgmq','pg_cron');
  create extension if not exists pgmq cascade;   -- nesmí hlásit 42725
  ```
  Když spadne: použij tag, kde je after-create opravený (viz `versions.md` +
  issue), nebo nouzově `alter extension pgmq drop function pgmq.drop_queue(text);`
  a znovu `create extension`. **Nikdy neměň na non-Supabase Postgres image** —
  přišel bys o Supabase role/init a rozbil Auth/Storage.

---

## Migrace dat z cloud Supabase (restore do self-hosted)

Čerstvý `db` si role vytvoří sám init skripty → **naivní plný dump koliduje.** Pravidla:
1. **Role NErestoruj.** `anon`/`authenticated`/`service_role`/`authenticator`/
   `supabase_admin`/… už existují a hesla mají z `POSTGRES_PASSWORD`. Nikdy
   nerestoruj `CREATE ROLE`/`ALTER ROLE … PASSWORD` z cloud dumpu. Dumpuj
   `--no-owner --no-privileges`.
2. **Supabase-managed schémata NErestoruj** (DDL `auth`/`storage` vlastní service
   admini; `realtime`/`_realtime`/`_analytics`/`_supabase`/`supabase_functions`/
   `net`/`graphql`/`extensions`/`pgsodium`/`vault`/`cron` nech být). Přenášíš jen
   **řádky dat** toho, co potřebuješ.
3. **Co reálně migrovat:**
   - `public` schéma: plné (`pg_dump --schema=public --no-owner --no-privileges`) —
     RLS policies + granty na `anon`/`authenticated`/`service_role` sednou, role existují.
   - Auth uživatelé: `--data-only --table='auth.users' --table='auth.identities'`
     (+ `mfa_*`/`sessions` dle potřeby). Hesla jsou bcrypt → přenositelná, žádné resety.
   - Storage: `--data-only --table='storage.buckets' --table='storage.objects'`
     (+ bajty souborů zvlášť do FS volume / R2).
4. **JWT secret:** chceš-li, aby existující refresh tokeny/JWT dál platily, nastav
   self-hosted `JWT_SECRET` = cloud projektu a reuse jeho anon/service klíče. Jinak
   vygeneruj nový a updatuj env appky (staré session zaniknou → jen re-login, hesla platí).
5. **Ownership:** restore `--no-owner` → objekty vlastní `postgres`/`supabase_admin`
   (správně). Pak ověř granty (`grant usage on schema public to anon, authenticated, service_role;`).
6. **Pořadí:** `public` schéma+data → auth data → storage data → `analyze`. Jako
   `supabase_admin` uvnitř kontejneru (`docker compose exec db psql -U supabase_admin -d postgres`).
   **Napřed snapshot/backup cílového volume a nacvič na zahoditelné kopii** — špatně
   zacílený restore do managed schémat umí zaseknout Auth/Storage.

Obecná metodika + ověření/cutover/rollback: `docs/migrations/supabase-postgres-data-migration.md`.

---

## Bezpečnost (checklist)

- [ ] `JWT_SECRET`, `ANON_KEY`, `SERVICE_ROLE_KEY` regenerovány (ne demo!). Anon/service
      = JWT podepsané ostrým `JWT_SECRET`.
- [ ] `SECRET_KEY_BASE` (≥64), `VAULT_ENC_KEY`, `PG_META_CRYPTO_KEY`, `REALTIME_DB_ENC_KEY`
      (přesně 16) regenerovány.
- [ ] `DASHBOARD_PASSWORD` silné + **`/` a `/pg/*` zablokované na CF hraně**.
- [ ] `analytics`/`vector` vynechané (RCE/log leak surface).
- [ ] `supavisor` odebraný (nebo jeho host porty na loopback/Tailscale). `db` bez host portu.
- [ ] Jen Kong `:8000` v tunelu; `:8443` netřeba.
- [ ] Phone signup/autoconfirm `false`.
- [ ] Reálný SMTP (Seznam 587), ověřený odesílatel (SPF/DKIM).
- [ ] `.env` gitignored; secrets přes Dokploy UI.

## RAM

| Sestava | Idle RAM |
|---|---|
| minimální (db, kong, auth, rest, realtime, storage, imgproxy) | ~0,9–1,4 GB |
| + Studio + meta | ~1,3–1,8 GB |
| vše vč. analytics/vector/supavisor/functions | ~2,5–4 GB |

Docs uvádí min 4 GB / doporučeno 8 GB. Server (32 GB) to utáhne vedle ostatních
homelab služeb; **analytics vynechej** kvůli rezervě. Pro víc projektů zvaž
**oddělenou Supabase instanci na produkční projekt** (Freio vlastní, izolovaná).
