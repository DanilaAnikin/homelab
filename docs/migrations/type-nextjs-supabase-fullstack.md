# Typ: Next.js + Supabase-native fullstack (supabase-js + RLS)

**Projekty:** claude-trader, dentallocal, explain-and-act (API), life-admin-agent
(+ Freio, ale ten má vlastní `migrate-freio.md`).

Společný znak: data se čtou/zapisují přes **`supabase-js` (`.from()`, `.rpc()`)**,
přihlašování přes **Supabase Auth**, autorizace přes **RLS s `auth.uid()`**. Někdy
+ Storage / Realtime. To znamená, že Supabase tu není jen DB — je to celá vrstva.

> **Doporučení pro tento typ: Strategie A (self-hosted Supabase),** pokud appka
> reálně spoléhá na RLS (dentallocal ~72 policies, explain-and-act). U jednodušších
> (claude-trader single-user, life-admin-agent co má `pg` path) jde i Strategie B.

---

## Postup — Strategie A (self-host Supabase), doporučeno pro RLS-heavy

1. Rozjeď Supabase stack na Dokploy → `self-hosted-supabase.md`.
2. Dumpni `public` + `auth` + `storage` z cloudu, restore do self-hosted →
   `self-hosted-supabase.md` (auth i RLS fungují 1:1, žádné resety).
3. Zkopíruj Storage soubory → `storage-to-r2.md` (nebo nech self-hosted Storage backend).
4. **Repoint env** (`SUPABASE_URL`, `ANON_KEY`, `SERVICE_ROLE_KEY`) na náš gateway.
5. GoTrue SMTP → LaunchMail (potvrzovací/reset maily jdou přes nás).
6. App-level e-maily (Resend) → LaunchMail → `email-to-launchmail.md`.
7. Deploy na Dokploy → `deploy-to-dokploy.md`, doména přes tunnel.
8. Ověř: login existujícím účtem, čtení dat pod RLS, klíčový flow, e-mail dorazí.

## Postup — Strategie B (holý Postgres + Better Auth), pro jednodušší

1. Migrace dat: `supabase-postgres-data-migration.md` (jen `public`).
2. Auth: `supabase-auth-to-better-auth.md` (zachovej UUID, bcrypt bez resetů).
3. **RLS → autorizace v kódu:** projdi KAŽDÝ `supabase-js` dotaz, co dnes spoléhá
   na RLS (cookie/anon-key čtení), a přidej explicitní `WHERE user_id = session.user.id`.
   `supabase-js` `.from()` přepiš na `pg`/Drizzle. **Nejrizikovější krok — pečlivě.**
4. `.rpc()` funkce → volej přes SQL/Drizzle (bez PostgREST). Funkce co používají
   `auth.uid()` přepiš na parametr `userId`.
5. Realtime (pokud je) → nahraď (SSE/polling/LISTEN-NOTIFY nebo self-host Realtime).
6. Storage → R2 (`storage-to-r2.md`). E-mail → LaunchMail. Deploy na Dokploy.

## Per-projekt specifika

### claude-trader (🟡 medium)
- Supabase Auth (pw + magic link + OAuth) + RLS `auth.uid() = user_id` + **Realtime**
  na `user_configs` (detekce start/stop tradingu).
- **Worker na Railway** → přesuň jako Dokploy Application (bez domény); mluví s DB
  service-role.
- E-mail: **2 místa** `fetch("https://api.resend.com/emails")` (worker/notifications.js,
  api/notifications/test) → LaunchMail. Pozn.: Resend klíč je **per-user, šifrovaný**
  v `user_configs.resend_api_key` — rozhodni: buď jeden sdílený LaunchMail token
  (zahoď per-user pole), nebo per-user tokeny. **`ENCRYPTION_KEY` přenes 1:1** (jinak
  se ty klíče nedešifrují).
- Realtime → při Strategii B nahraď polling/LISTEN-NOTIFY.

### dentallocal (🔴 hard — Strategie A jednoznačně)
- Monorepo, **6 apek** (`bistro/dental/auto/fit/salon/vet`) sdílí `packages/core` a
  **jednu identitu**. **~72 RLS policies** = jediná vrstva izolace nájemníků.
- Auth: magic-link + Google OAuth. Storage: private `reports` bucket (signed URL).
- E-mail: **Resend SDK + react-email** → jeden chokepoint `sendEmail` → LaunchMail
  (react-email už renderuje HTML, mapování 1:1). `email_suppression` je app-side.
- Deploy: 6 Dokploy Application služeb (různý Root Directory / `NEXT_PUBLIC_VERTICAL`).
- Inngest joby + Vercel Cron → Dokploy Schedules.

### explain-and-act (🔴 hard — pozor na mobil)
- **Mobilní appka sahá na Supabase PŘÍMO** (`supabase.from("documents")`, `.storage`,
  `supabase.auth`) — jen RLS ji chrání. Při Strategii B bys musel doplnit API
  endpointy pro list/delete dokumentů + auth a z mobilu `supabase.*` odstranit.
  **Při Strategii A mobil funguje dál** (jen repoint `EXPO_PUBLIC_SUPABASE_URL`).
- Auth: email/pw. Storage: private `documents` (per-user složky). RPC `consume_scan`/
  `refund_scan`. Transakční maily = GoTrue (při A jdou přes LaunchMail SMTP).

### life-admin-agent (🟢 medium — nejsnazší z této skupiny)
- **`packages/db` už má `pg` provider** (`DB_PROVIDER=postgres` + `POSTGRES_URL`,
  ruční SQL) → DB migrace = jen přepnout provider, žádná změna kódu.
- **E-mail už je nodemailer SMTP** (`EMAIL_PROVIDER=smtp`, `SMTP_URL`) → nasměruj na
  LaunchMail SMTP, hotovo.
- Zbývají **2 blokery:** Auth je hardwired na GoTrue (buď self-host GoTrue, nebo
  Better Auth) a Storage má jen mock/Supabase provider (dopsat R2 provider —
  přílohy mailů letadlům jdou přes storage path). `supabase/docker/00_bootstrap.sql`
  už stubuje `auth.users`/`auth.uid()`/`storage` na holém PG.

## Univerzální ověření (tento typ)
- [ ] Login existujícím účtem bez resetu; RLS/authz nepustí cizí data
- [ ] `.rpc()` funkce vrací správně; Realtime (je-li) živě aktualizuje
- [ ] Storage: signed URL i public soubory fungují
- [ ] E-maily (app i auth) přes LaunchMail dorazí do inboxu
- [ ] Data: počty řádků sedí; Supabase necháno jako fallback
