# Migrace autentizace: Supabase Auth → self-hosted Better Auth

Nejtěžší část celé migrace — ne databáze, ale **auth**. Šest projektů stojí na
Supabase Auth (GoTrue): claude-trader, ripieno, dentallocal, life-admin-agent,
agent-farm, loot (ten už je rozdělaný sám). leadcrm a loot mají vlastní auth
(snadné). hummy nemá auth vůbec. **Žádný projekt nepoužívá Clerk** (dřívější
domněnka byla mylná).

Cíl: identita a hesla žijí v **naší** Postgres DB, spravuje je **Better Auth**.
Zásada: **nikdo nesmí být nucen resetovat heslo** a **žádná doménová data nesmí
osiřet**.

---

## Proč je to citlivé — 3 věci, co musí sednout

1. **Zachovat UUID uživatelů.** Ve všech projektech doménová data odkazují na
   `auth.users(id)` (přímo, nebo přes zrcadlo `public.users`/`profiles.id`, které
   se `id` rovná). Better Auth **musí dostat stejné UUID jako `user.id`** — jinak
   se rozbijí VŠECHNY FK (`owner_id`, `user_id`, memberships, invited_by, …).
   Better Auth umožňuje při importu dodat vlastní `id`. Použij to.

2. **Přenést hesla bez resetu.** Supabase GoTrue ukládá hesla jako **bcrypt**
   (`$2a$…`) v `auth.users.encrypted_password`. Better Auth default je scrypt,
   ale umí **vlastní hasher** (`emailAndPassword.password.hash/verify`). Zapoj
   bcrypt verifikátor a naimportuj hashe do Better Auth `account`
   (`providerId='credential'`) → **žádné resety**.

3. **RLS zmizí → autorizace do aplikace.** Všechny projekty spoléhají na RLS
   policies s `auth.uid()`. Better Auth nemá DB-side identitu, takže **viditelnost
   řádků se musí vynutit v dotazech aplikace** (WHERE `owner_id = session.user.id`).
   Bez toho by se data „obnažila". loot to má popsané ve svém `MIGRATION.md`.

---

## 1) Nasaď Better Auth do naší DB

Better Auth běží uvnitř appky (Next.js route handler) a ukládá do **naší
Postgres** (přes Drizzle/pg adapter). Tabulky, které si vytvoří: `user`,
`session`, `account`, `verification` (+ `organization`, `member`, `invitation`
při org pluginu).

```ts
// lib/auth.ts
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { db } from "@/db";
import { compare } from "bcryptjs"; // ověření importovaných Supabase hashů

export const auth = betterAuth({
  database: drizzleAdapter(db, { provider: "pg" }),
  emailAndPassword: {
    enabled: true,
    // Ověř bcrypt (Supabase) i nové hashe. Nové účty ať Better Auth hashuje
    // svým defaultem; pro import stačí verify umět bcrypt.
    password: {
      hash: async (pw) => /* nech default nebo bcrypt.hash */,
      verify: async ({ password, hash }) => compare(password, hash),
    },
  },
  // multi-tenant projekty (ripieno, dentallocal): plugin organization
  // plugins: [organization({ ... })],
  // OAuth: socialProviders: { github: {...}, google: {...} },
});
```

## 2) Vytáhni Supabase auth uživatele

**Cloud Supabase projekty** (claude-trader, ripieno, dentallocal, agent-farm) —
`auth` schéma není v běžném `--schema=public` dumpu, musíš ho vzít zvlášť:
```bash
pg_dump "$SUPABASE_URL" --schema=auth --data-only -Fc -f auth_users.dump
# nebo cíleně jen potřebná pole:
psql "$SUPABASE_URL" -Atc \
 "COPY (SELECT id, email, encrypted_password, email_confirmed_at, created_at
        FROM auth.users) TO STDOUT WITH CSV HEADER" > auth_users.csv
```
**Self-hosted** (life-admin-agent, a loot po přesunu na Dokploy) — čteš
`auth.users` přímo SQL, žádná exportní gymnastika.

> Ověř formát hesla v `encrypted_password`: reálné GoTrue = bcrypt `$2a$…`.
> U life-admin-agent (vlastní Docker shim) potvrď, že bootstrap taky seeduje
> bcrypt, ne plaintext/jiný formát — jinak uprav verify().

## 3) Naimportuj do Better Auth (zachovej UUID + hash)

Napiš jednorázový skript, který pro každý řádek `auth.users` vloží:
```
user     : { id: <STEJNÉ UUID>, email, emailVerified: !!email_confirmed_at, createdAt }
account  : { userId: <STEJNÉ UUID>, providerId: "credential",
             password: <bcrypt hash z encrypted_password> }
```
- **`id` = původní UUID** (splní bod 1 — FK nezosiří).
- OAuth-only uživatelé (bez hesla) → nemají `credential` account; propojí se
  přes e-mail při prvním OAuth loginu (social provider). Viz bod 6.

## 4) Přesměruj zrcadlící tabulku / trigger

Projekty mají `public.users`/`profiles.id → auth.users` + trigger
`handle_new_user()`, co zakládá profil při vzniku auth uživatele. Po migraci:
- **Zdroj pravdy je Better Auth `user`.** Zrcadlo `profiles` buď:
  - (a) nech, ale FK přepni na `better_auth.user(id)` (stejné UUID → sedí), a
    profil zakládej v app callbacku Better Auth (`databaseHooks.user.create.after`),
    ne DB triggerem na `auth.users` (ten už neexistuje), nebo
  - (b) zruš zrcadlo a čti profilová pole přímo z `user` + doménových tabulek.
- Trigger `handle_new_user()` na `auth.users` **smaž** (tabulka zanikla).

## 5) RLS → autorizace v aplikaci

Pro každou tabulku, co měla RLS `auth.uid() = user_id`:
- Ve všech dotazech přidej `WHERE user_id = session.user.id` (resp. org scope).
- RLS na naší DB buď **vypni** (`DISABLE ROW LEVEL SECURITY`), nebo — bezpečnější
  „defense in depth" — nech RLS a nastav ji podle `current_setting('app.user_id')`,
  které appka setne na začátku transakce. Pro start stačí app-layer + vypnutá RLS,
  ale **musíš mít jistotu, že každý dotaz filtruje vlastnictví**. Projdi to pečlivě.

## 6) OAuth a multi-tenant

- **OAuth providery** znovu nastav v Better Auth `socialProviders`: ripieno =
  GitHub, dentallocal = Google, claude-trader = (provider-agnostic, ověř za běhu).
  OAuth-only účty (bez hesla) se po prvním přihlášení **spárují přes e-mail** s
  naimportovaným `user` (stejný e-mail → stejný účet, UUID zachováno).
- **Multi-tenant** (ripieno: orgs+memberships+teams+invites;
  dentallocal: orgs+memberships+invitations přes 6 apek) → **Better Auth
  `organization` plugin**. Naimportuj organizace/memberships se **stejnými UUID**;
  `auth.admin.inviteUserByEmail` / `generateLink` přepiš na Better Auth invitation
  flow. Role (`owner/admin/editor/viewer`, resp. `member/admin`) namapuj na role pluginu.

## 7) Ověření + rollback

- [ ] Počet `user` v Better Auth == počet `auth.users` v Supabase.
- [ ] **Login existujícím účtem heslem** → funguje bez resetu (bcrypt verify).
- [ ] **OAuth login** → spáruje se se správným existujícím účtem (ne duplicita).
- [ ] Doménová data se zobrazují správnému uživateli (žádné osiřelé/cizí řádky).
- [ ] Multi-tenant: členství, role a pozvánky sedí.
- [ ] Reset hesla / magic link (pokud projekt používá) jdou přes **LaunchMail**.
- **Rollback:** dokud nesmažeš Supabase projekt, přepnutím env zpět na Supabase
  Auth se vrátíš. Migruj auth v maintenance okně spolu s DB (viz
  `supabase-postgres-data-migration.md` cutover B), ať je jeden konzistentní řez.

---

## Per-projekt obtížnost (mapa)

| Projekt | Auth dnes | Obtížnost | Klíčové |
|---------|-----------|-----------|---------|
| **leadcrm** | vlastní (PBKDF2 + HMAC cookie) | 🟢 triviální | vlastní `app_users`, PBKDF2 hashe portovatelné, identita = username (přidej email nebo username plugin), žádné UUID FK |
| **loot** | Supabase Auth, už půl-migrované na argon2+sessions | 🟢 nízká | dokončit dle jeho `MIGRATION.md`; nahradit vlastní `sessions` Better Authem; zachovat UUID; bcrypt hashe ze Supabase kvůli starým účtům |
| **life-admin-agent** | Supabase Auth, ale **self-hosted** (vlastní Postgres) | 🟢 nízká–střední | `auth.users` (vč. `encrypted_password`) čteš přímo SQL → přímý import; ověř bcrypt; single-user |
| **claude-trader** | Supabase Auth | 🟡 střední | export `auth.users`, bcrypt import, magic-link + OAuth + reset flows; RLS→app; single-user; POZOR na `ENCRYPTION_KEY` (per-user Resend klíče) |
| **agent-farm** | Supabase Auth | 🟡 střední | standardní import; `inviteUserByEmail` přes Better Auth; role member/admin; `is_admin()`→app; UUID (profiles, billing, autonomy, invited_by) |
| **ripieno** | Supabase Auth, **multi-tenant** | 🟠 střední–vysoká | organization plugin (orgs/memberships/teams/invites), GitHub OAuth, `auth.admin.*`→Better Auth; zachovat UUID |
| **dentallocal** | Supabase Auth, **multi-tenant, 6 apek 1 identita** | 🔴 vysoká | největší plocha: org plugin, Google OAuth, invitations, sdílený identity store napříč 6 apps; zachovat UUID všude |
| **hummy** | žádný | ⚪ N/A | nemá účty; kdyby přidal, greenfield Better Auth |

> **Doporučené pořadí migrace authu:** leadcrm/loot (nácvik na snadných) →
> life-admin-agent (self-hosted, přímý import) → claude-trader / agent-farm
> (single-user cloud) → ripieno → dentallocal (nejsložitější). Freio zvlášť
> (`migrate-freio.md`).
