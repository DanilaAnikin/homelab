# Bezpečná migrace dat: Supabase → náš self-hosted PostgreSQL

Páteřní metodika pro **přesun databáze bez ztráty jediného řádku**. Odkazují se
na ni všechny per-projekt dokumenty. Zlaté pravidlo: **nikdy nemažeme zdroj,
dokud cíl není ověřený a pár dní v provozu.** Supabase zůstává celou dobu živý
jako fallback.

---

## 0) Co je vlastně v Supabase (než začneš)

Supabase = PostgreSQL + vrstvy. V dumpu narazíš na víc než jen svoje tabulky:

| Schéma | Co je uvnitř | Migrujeme? |
|--------|--------------|-----------|
| `public` | **tvoje aplikační data** | ✅ ano, hlavní věc |
| `auth` | Supabase Auth uživatelé (`auth.users`, …) | ⚠️ jen když projekt používá Supabase Auth — viz `supabase-auth-to-better-auth.md` |
| `storage` | metadata Supabase Storage (buckety, objekty) | ⚠️ soubory zvlášť — viz `storage-to-r2.md` |
| `extensions`, `graphql`, `realtime`, `vault`, `pgsodium` | Supabase infra | ❌ nemigrujeme; nahrazujeme vlastní službou |

**Extensions** (musíme mít i u nás, jinak restore spadne): typicky `uuid-ossp`,
`pgcrypto`, `pg_stat_statements`, u AI projektů `vector` (pgvector). Zjistíš:
```sql
SELECT extname FROM pg_extension;
```

---

## 1) Připoj se k Supabase pro dump

Supabase → Project → **Settings → Database**. Použij **Session pooler** /
direct connection (port 5432), NE transaction pooler (6543) — pg_dump potřebuje
session mode. Connection string:
```
postgresql://postgres.<ref>:<HESLO>@aws-0-<region>.pooler.supabase.com:5432/postgres
```
> Tip: vezmi si i `db.<ref>.supabase.co:5432` direct string, pokud pooler zlobí.

## 2) Vytvoř plný, konzistentní dump (read-only, nic nerozbije)

```bash
# jen tvoje aplikační data (nejběžnější případ — projekt NEpoužívá Supabase Auth):
pg_dump "$SUPABASE_URL" \
  --format=custom --no-owner --no-privileges \
  --schema=public \
  --exclude-table='public.schema_migrations' \
  -f freio_public.dump

# pokud používáš i Supabase Auth (potřebuješ auth.users kvůli FK):
pg_dump "$SUPABASE_URL" --format=custom --no-owner --no-privileges \
  --schema=public --schema=auth -f freio_full.dump
```

- `--no-owner --no-privileges` = zahodí Supabase role/vlastnictví (u nás jiné role).
- `--format=custom` = umožní selektivní `pg_restore` a paralelizaci.
- **Je to čtení — na produkci Supabase nic nemění.** Klidně opakuj.

Zvlášť si vytáhni **jen data** (pro pozdější delta/re-sync bez schématu):
```bash
pg_dump "$SUPABASE_URL" --data-only --schema=public -Fc -f freio_data.dump
```

## 3) Připrav cílovou databázi (náš Postgres)

```bash
ssh anakin@homelab
/srv/homelab/scripts/newdb.sh freio            # vytvoří DB + usera + hesla
# nainstaluj potřebné extensions do té DB (podle kroku 0):
sudo docker exec -it shared-postgres psql -U postgres -d freio \
  -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS vector;'
```

## 4) Restore do našeho Postgresu

Přes SSH tunel z notebooku (`ssh -L 5432:localhost:5432 anakin@homelab`):
```bash
pg_restore --no-owner --no-privileges --role=freio \
  --schema=public \
  -d "postgres://freio:<HESLO>@localhost:5432/freio" \
  --jobs=4 freio_public.dump
```
Případné chyby o chybějících Supabase objektech (RLS na `auth.uid()`, cizí
funkce) jsou **očekávané** — viz krok 5. Restore neprerušuj, projeď a pak vyřeš.

## 5) Vyřeš Supabase-specifické věci (nejčastější landminy)

| Problém | Řešení |
|---------|--------|
| **RLS policies** odkazují na `auth.uid()` / `auth.role()` | Pokud se appka připojuje důvěryhodným server connection (Prisma/Drizzle přes náš connection string), RLS se neuplatní na superuser/owner a appka autorizaci řeší v kódu. Buď **RLS vypni** (`ALTER TABLE x DISABLE ROW LEVEL SECURITY`), nebo policies přepiš na náš auth. **Nikdy** nespoléhej na to, že RLS „prostě funguje" bez Supabase. |
| **FK na `auth.users(id)`** | Když domain tabulky odkazují na Supabase auth uživatele: migruj `auth.users` taky (nebo remapuj na náš Better Auth `user.id`) — detail v `supabase-auth-to-better-auth.md` a `clerk-to-better-auth.md`. |
| **Sekvence / identity** | `pg_restore` je přenese, ale ověř `SELECT setval(...)` u ručně naplněných. Zkontroluj, že `nextval` sedí nad max(id). |
| **`storage.objects` URL v datech** | Sloupce s `https://<ref>.supabase.co/storage/...` → přepiš na naše R2 URL po migraci souborů (`storage-to-r2.md`). |
| **Edge Functions / Database Webhooks / Realtime** | Nemigrují se — přepiš jako API routes / app logiku na serveru. |
| **`gen_random_uuid()` / `uuid_generate_v4()`** | Vyžaduje `pgcrypto` / `uuid-ossp` extension (krok 3). |
| **pgvector embeddings** | `CREATE EXTENSION vector` před restorem; jinak `type "vector" does not exist`. |

## 6) OVĚŘENÍ integrity (nepřeskakuj — tady se pozná, že nic nechybí)

```bash
# a) počty řádků na každé tabulce — MUSÍ sedět zdroj vs cíl
for t in $(psql "$SUPABASE_URL" -Atc "SELECT tablename FROM pg_tables WHERE schemaname='public'"); do
  s=$(psql "$SUPABASE_URL" -Atc "SELECT count(*) FROM public.\"$t\"");
  d=$(psql "$LOCAL_URL"    -Atc "SELECT count(*) FROM public.\"$t\"");
  [ "$s" = "$d" ] && echo "OK   $t  $s" || echo "MISMATCH $t  supabase=$s  local=$d";
done
```
- **Spot-check kritických záznamů** (u Freio: pár reálných klientů — sedí pole?).
- **FK integrita:** žádné osiřelé řádky (`SELECT` s LEFT JOIN kde child bez parenta).
- **Sekvence:** `SELECT max(id) FROM t;` vs `SELECT last_value FROM t_id_seq;`.
- Ulož si výstup porovnání počtů jako důkaz před cutoverem.

## 7) Cutover strategie (podle rizika projektu)

**A) Nízké riziko (hobby, málo/žádní uživatelé):** jednorázově.
1. Dump → restore → ověř (kroky 2–6).
2. Přepni `DATABASE_URL` v appce na náš Postgres, redeploy.
3. Sleduj pár dní; Supabase nech pauznutý jako fallback.

**B) Vysoké riziko (Freio — živí klienti, hodně dat):** okno + delta.
1. **Předběžný dump/restore/ověření** v klidu dopředu (bez výpadku, jen čtení).
2. Naplánuj **krátké maintenance okno** (např. v noci): appku přepni do
   read-only / maintenance režimu, ať nikdo nezapisuje.
3. **Finální delta dump** jen dat (`--data-only`) od času předběžného dumpu, nebo
   raději celý čerstvý `--data-only` restore do prázdné kopie (rychlejší než řešit delty).
4. Ověř počty (krok 6) na čerstvých datech.
5. Přepni `DATABASE_URL` → náš Postgres, vypni maintenance, redeploy.
6. **Smoke test s reálným klientským účtem** (login, načtení dat, klíčová akce).
7. Supabase **NEMAZAT** — nech read-only jako fallback min. 1–2 týdny.
   Pauznout/smazat až po prokázaně bezchybném provozu + záloze v našem R2.

> Pro úplně nulový výpadek by šla logická replikace (Supabase → náš PG přes
> `CREATE SUBSCRIPTION`), ale pro tvůj rozsah je okno v noci jednodušší a
> bezpečnější. Delta přístup dokumentuju ve `migrate-freio.md`.

## 8) Po přepnutí

- [ ] První noc po cutoveru ověř, že projekt je v **naší** denní záloze
      (`rclone ls r2:homelab-backups | grep freio`).
- [ ] Zkontroluj slow-query log (`log_min_duration_statement=500`) — Supabase
      měl jiné indexy? Dořeš chybějící indexy.
- [ ] Odstroj z kódu supabase-js klienty a env (`SUPABASE_*`), pokud už nejsou
      potřeba (často zůstávaly jen kvůli Storage/Auth — ty řeší jiné dokumenty).
- [ ] Teprve po 1–2 týdnech: Supabase projekt pauzni (free tier to udělá sám).

## Rollback (kdyby cokoli)

Protože jsme Supabase nechali netknutý: **přepni `DATABASE_URL` zpět** na Supabase
a redeploy. Data na Supabase jsou pořád ta původní (jen jsme z nich četli). Žádná
ztráta. Proto se zdroj nikdy nemaže dřív, než je nový domov prověřený.
