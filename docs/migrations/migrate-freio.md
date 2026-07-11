# Migrace Freio na vlastní stack — kompletní runbook (produkce + klienti)

Freio je **největší a nejrizikovější migrace**: živá SaaS (freio.cz) s platícími
klienty, hodně dat v Supabase, hluboce Supabase-native (RLS na skoro všem, GoTrue
Auth, Storage), Resend s marketingovými audiences, Stripe **live** platby + Google
Play, Firebase push a **Android appka** na stejné DB. **Tady se nesmí ztratit ani
řádek a nesmí to spadnout klientům pod rukama.**

> Freio dělej **až úplně poslední**, po odladění postupu na ostatních projektech.
> Nikdy ne jako první migraci.

---

## Fakta o Freio (z auditu kódu)

- **Next.js 14 App Router**, `output: standalone`, single app (ne monorepo), npm,
  Vercel + **1 cron** (`/api/internal/lifecycle/run`, denně 7:00, lifecycle maily).
- **DB = Supabase, ale i Auth i Storage.** Schéma je **ručně psané raw SQL**
  (`supabase/*.sql` + `supabase/migrations/*.sql`), aplikované ručně. **Žádné Prisma,
  žádný ORM.** Data se čtou přes `supabase-js` `.from()`/`.rpc()` s **vynucenou RLS**
  (anon key + user JWT), a **service-role** klíčem se RLS obchází ve webhoocích/cronu/adminu.
- **Auth = Supabase GoTrue** (email/heslo + Google OAuth, cookie session přes
  `@supabase/ssr`, Bearer token pro Android). Profil se plní **triggerem
  `handle_new_user()` na `auth.users`** z `raw_user_meta_data`.
- **~95 odkazů na `auth.users`/`auth.uid()`.** 8 tabulek má **FK přímo na
  `auth.users(id)`** (`public.users.id`, `stripe_customers`, `purchases`,
  `user_subscriptions`, `user_unlocked_tests`, `test_attempts`, `payment_audit.changed_by`,
  `campaign_redemptions`). **User ID = GoTrue UUID všude.**
- **E-mail = Resend** (`business@freio.cz`) + **Contacts/Audiences/Contact Properties**
  (marketing CRM) + **automatické přílohy legal PDF** ke každému mailu. ~18 šablon
  (HTML stringy, ne react-email). GoTrue posílá navíc **potvrzovací + reset maily**.
- **Storage = 1 bucket** `marketing-assets` (public, loga škol). URL v
  `approved_institutions.logo_path`.
- **Platby = Stripe live** (Checkout + subscriptions + Billing Portal, webhook s
  idempotencí `stripe_events`, rotace přes `STRIPE_WEBHOOK_SECRETS`) **+ Google Play**
  (druhý provider, `billing_provider` sloupce). **Firebase** push (FCM).
- **Android appka** (`freio-android/`, Kotlin, samostatné repo) volá stejné API
  přes Supabase Bearer tokeny.
- **Extensions:** `uuid-ossp` + `pgcrypto`/builtin. **Žádný pgvector, žádný PostGIS**
  → nic cloud-exkluzivního. Materialized view `leaderboard_cache` +
  `refresh_leaderboard_cache()` (potřebuje scheduled refresh). ~30 plpgsql funkcí
  (většina `SECURITY DEFINER`, závislé na `auth.uid()`).
- **Bez Realtime, bez Edge Functions.**

---

## Rozhodnutí strategie: **A — self-hostovaný Supabase.** Jednoznačně.

Proč ne holý Postgres + Better Auth pro Freio:
- ~95 `auth.uid()` RLS policies + `SECURITY DEFINER` helpery = přepsat všechno a
  přesunout autorizaci do kódu = **obrovská plocha, kde se snadno „obnaží" cizí data
  platících klientů**. Nepřijatelné riziko pro produkci.
- 8 FK na `auth.users` + trigger `handle_new_user` + Android Bearer tokeny + GoTrue
  auth maily = auth je zapletený do DB i mobilu.

Se **Strategií A** (self-host Supabase) se `auth.users`, RLS i Storage přenesou 1:1,
kód se skoro nemění, klienti nic nepoznají, hesla fungují bez resetů. To je pro
produkci s klienty **řádově bezpečnější**. (Na holý stack se dá přejít časem, v klidu.)

📄 Základ: `self-hosted-supabase.md`. Tady je Freio-specifický postup navíc.

---

## FÁZE 0 — Příprava (dny předem, ŽÁDNÝ výpadek, jen čtení/stavění)

- [ ] **Rozjeď izolovanou self-hosted Supabase instanci** pro Freio na Dokploy
      (`self-hosted-supabase.md`). Vlastní JWT projekt (nové `ANON`/`SERVICE_ROLE`).
      Postgres image s `uuid-ossp`+`pgcrypto` (pgvector netřeba).
- [ ] **GoTrue SMTP → LaunchMail** (potvrzovací + reset maily). Ověř odesílací
      doménu (DKIM) pro `freio.cz` / `mail.freio.cz` v LaunchMailu.
- [ ] Založ **R2 bucket** `freio-marketing-assets` (nebo nech self-hosted Storage volume).
- [ ] **Zkušební plný dump z cloud Supabase** (public+auth+storage) a **restore do
      self-hosted** → ověř, že projde a appka lokálně naběhne proti němu. Tenhle
      „nanečisto" běh odhalí problémy dopředu, bez dopadu na klienty.
- [ ] **Rotuj secrets** (Freio má live secrets commitnuté): Stripe live keys nech
      (jsou u Stripe), ale service-role JWT bude nový (self-hosted), `ADMIN_PASSWORD`,
      `EMAIL_PREFERENCES_SECRET`, Firebase/Google Play klíče drž jen v Dokploy env.
- [ ] **Android:** připrav build appky mířící na nový backend (`freio-android/`),
      ať je nasaditelný **v lockstepu** s cutoverem (viz Fáze 5).

## FÁZE 1 — E-mail (Resend → LaunchMail), předběžně

Freio má e-mail složitější (Contacts/Audiences + PDF přílohy). Řeš odděleně:
- [ ] **Transakční + lifecycle maily** (welcome, payment, invite, consent, school-reco,
      lifecycle) → přesměruj `lib/email/resend.ts` `sendEmail` na **LaunchMail**
      (`email-to-launchmail.md`). Šablony jsou HTML stringy → přímo pošli `html`.
      **PDF přílohy** (`getDefaultLegalPdfAttachments()`) → LaunchMail podporuje
      `attachments` (base64) — přenes je.
- [ ] **Marketing Contacts/Audiences/Properties** (`upsertContact`, contactProperties):
      LaunchMail má audiences, ale mapování custom properties je práce. **Rozhodnutí:**
      buď (a) marketingovou list-sync vrstvu dočasně **nech na Resendu** (jen ty
      `contacts.*` volání), a transakční maily přepni na LaunchMail — hybrid, nejmenší
      riziko; nebo (b) přenes kontakty do LaunchMail audiences později jako samostatný
      krok. **Pro cutover stačí (a).**
- [ ] **GoTrue auth maily** (potvrzení/reset) jdou přes LaunchMail už z Fáze 0.
- [ ] `EMAIL_PREFERENCES_SECRET` (unsubscribe tokeny) přenes 1:1.

## FÁZE 2 — Storage soubory

- [ ] `rclone copy` bucketu `marketing-assets` cloud → R2/self-hosted
      (`storage-to-r2.md`), `rclone check` na ověření.
- [ ] Přeber, že `approved_institutions.logo_path` má **mix**: Supabase URL (z uploadů)
      i lokální `/logos/partners/*.png` (5 seed řádků). Přepiš **jen** ty se Supabase URL.
- [ ] `next.config.mjs` `images.remotePatterns` (`**.supabase.co`) + CSP
      (`connect-src`/`img-src` `*.supabase.co`) → přepiš na naši Storage/R2 doménu.

## FÁZE 3 — Kód repoint (Strategie A, minimum změn)

- [ ] Env: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
      `SUPABASE_SERVICE_ROLE_KEY` → náš self-hosted gateway + nové klíče.
- [ ] `lib/supabase/{client,server,admin,middleware,api-auth}.ts` **zůstávají** —
      supabase-js se jen připojí jinam. RLS, `.rpc()`, `auth.*`, `getApiUser()` (Bearer)
      fungují dál (GoTrue běží u nás).
- [ ] Trigger `handle_new_user()` a všechny `auth.*`/`SECURITY DEFINER` helpery jsou
      **v dumpu** a v self-hosted GoTrue **fungují** — nic nepřepisuješ. (To je celá
      pointa Strategie A.)
- [ ] **Admin auth** (`lib/admin/auth.ts`, `ADMIN_EMAIL`/`ADMIN_PASSWORD` cookie) je
      nezávislý na Supabase — jen přenes env.
- [ ] **Stripe webhook:** endpoint zůstává; přenes `STRIPE_WEBHOOK_SECRET(S)` a v
      **Stripe dashboardu přidej nový webhook endpoint** na novou doménu appky (na
      Dokploy). Díky `stripe_events` idempotenci je bezpečné mít krátce oba. Middleware
      rewrite legacy `POST /` s `stripe-signature` zůstává.
- [ ] **Firebase push** (`FIREBASE_*`) + **Google Play** (`GOOGLE_PLAY_*`) — jen přenes
      env; jsou klíčované user UUID (zachováno) → fungují dál.

## FÁZE 4 — Cron / scheduled

- [ ] **Lifecycle cron** (`/api/internal/lifecycle/run`, denně 7:00) → Dokploy Schedule
      (POST s `CRON_SECRET`/`LIFECYCLE_CRON_SECRET`).
- [ ] **`refresh_leaderboard_cache()`** (materialized view `leaderboard_cache`) —
      cloud Supabase to nejspíš refreshoval jinak; nastav **pg_cron** v našem Postgresu
      nebo Dokploy Schedule, který zavolá `SELECT refresh_leaderboard_cache();` (jinak
      žebříček zamrzne).

## FÁZE 5 — Ostrý cutover (maintenance okno + Android lockstep)

Předpoklad: Fáze 0–4 odladěné „nanečisto", data ověřená na zkušebním restoru.

1. **Vyber tiché okno** (noc). Zapni na freio.cz **maintenance/read-only** (ať nikdo
   nezapisuje během finálního dumpu).
2. **Finální dump** cloud Supabase (`public`+`auth`+`storage`) → restore do
   self-hosted (čerstvá kopie). **Ověř počty řádků** (zdroj == cíl) na všech tabulkách
   + spot-check pár **reálných klientských účtů** (`supabase-postgres-data-migration.md` krok 6).
3. **Finální `rclone` sync** bucketu `marketing-assets` (delta od Fáze 2).
4. **Přepni `product_type` enum pozor** — už je v dumpu (má všechny 3 hodnoty vč.
   `practice_subscription`), takže OK; jen ověř, že enum existuje před daty.
5. **Deploy Freio na Dokploy** s novými env (Fáze 1–4). Doména `freio.cz` přes tunnel.
6. **Nasaď Android build** mířící na nový backend **současně** (lockstep) — jinak
   mobil s Bearer tokeny proti starému Supabase přestane fungovat. (Nebo drž starý
   Supabase živý pro staré verze appky, dokud neaktualizují.)
7. **Přepni Stripe webhook** na produkční (nový endpoint aktivní, starý můžeš krátce
   nechat — idempotence to ustojí).
8. Vypni maintenance.
9. **Smoke test s reálným klientským účtem:** login (bez resetu!), načtení testů a
   výsledků, koupě/subscription stav, přijetí e-mailu, admin panel.

## FÁZE 6 — Po cutoveru

- [ ] První noc: ověř, že Freio je v naší **R2 záloze** (`rclone ls r2:homelab-backups | grep freio`).
- [ ] Sleduj slow-query log — chybí nějaký index, co měl cloud? Dořeš.
- [ ] Sleduj Stripe webhooky (dorazí, `stripe_events` je claimuje), lifecycle cron,
      leaderboard refresh, doručitelnost mailů (LaunchMail logy → inbox, ne spam).
- [ ] **Cloud Supabase, Vercel i Resend nech běžet jako fallback ≥ 1–2 týdny.**
      Teprve po prokázaně bezchybném provozu + potvrzené záloze je pauzni/zruš.

## Landminy (checklist — každou vyřeš, nebo víš proč ne)

- [ ] **A. `auth.users` FK / UUID** — Strategie A přenáší `auth.users` 1:1 → UUID
      zachována, nic neosiří. (U Strategie B by tohle byl hlavní risk.)
- [ ] **B. Hesla (bcrypt)** — přenesou se v `auth.users` → login bez resetů.
- [ ] **C. Google OAuth identity** (`auth.identities`) — v dumpu; ověř Google OAuth
      redirect/callback URL v našem GoTrue configu + Google Cloud consolu.
- [ ] **D. `handle_new_user()` trigger** — přenese se a funguje (GoTrue běží).
- [ ] **E. RLS `auth.uid()`** — funguje (GoTrue běží). ✅ hlavní důvod pro Strategii A.
- [ ] **F. service-role vs anon duality** — zachována (dva klíče, dvě úrovně důvěry).
- [ ] **G. GoTrue auth maily** — přes LaunchMail (Fáze 0/1).
- [ ] **H. Resend Contacts/Audiences/PDF** — transakční na LaunchMail; marketing
      list dočasně na Resendu (hybrid) nebo přenést později.
- [ ] **I. Storage URL v DB** — přepsané (Fáze 2), config/CSP upravené.
- [ ] **J. `leaderboard_cache` refresh** — naplánovaný (Fáze 4).
- [ ] **K. `product_type` enum** — všechny 3 hodnoty přítomné před daty (dump je má).
- [ ] **L. matview/`UUID[]`/GIN/plpgsql** — standardní PG, přenesou se.
- [ ] **M. Android Bearer auth** — build v lockstepu (Fáze 5.6).
- [ ] **N. Secret rotace** — hotová (Fáze 0).

## Rollback

Protože cloud Supabase, Vercel i Resend zůstávají **netknuté a živé** až do
prokázaného úspěchu: rollback = **přepnout DNS/deploy zpět na Vercel + env zpět na
cloud Supabase** (+ Android na starý backend / starý build). Žádná ztráta dat —
z cloudu jsme jen četli. Proto se nic nemaže dřív než po 1–2 týdnech čistého provozu.
