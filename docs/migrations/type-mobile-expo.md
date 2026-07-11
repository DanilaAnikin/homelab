# Typ: Expo / React Native mobil

**Projekty:** hummy, explain-and-act (mobilní část).

## 🚨 Železné pravidlo: mobil NIKDY nesahá na DB přímo

Mobilní appka je na zařízeních uživatelů — nesmí mít přímý přístup k naší databázi.
Vždy mluví s **API vrstvou** (server), a ta teprve s DB. Připojovací string do
Postgresu se nikdy nesmí ocitnout v mobilní appce.

Stav dnes:
- **hummy** — ✅ **správně**: appka volá **Supabase Edge Functions** (Deno) přes
  HTTPS, ne DB přímo. API vrstva existuje.
- **explain-and-act** — ⚠️ **porušuje pravidlo**: mobil dělá `supabase.from("documents")
  .select()/.delete()` a `supabase.storage...remove()` a `supabase.auth.*` **přímo**
  (chrání to jen RLS + anon key). Jen AI operace jdou přes `apps/api`.

---

## hummy (🟢 easy)

- **Co migrovat:** samotná DB je snadná (plain SQL, jen service-role z edge funkcí,
  RLS bez policies, žádný Auth/Storage/Realtime). Reálná vazba je **Deno Edge
  Functions runtime** (`identify`, `event`, `entitlement`, `stripe-webhook`).
- **Dvě cesty:**
  1. **Strategie A** — self-hosted Supabase včetně **Edge Runtime** kontejneru;
     nasaď funkce (`supabase functions deploy` proti našemu stacku). Appka jen
     přesměruje `EXPO_PUBLIC_SUPABASE_URL` a nechá `/functions/v1/...` cestu +
     anon-key hlavičku. Nejmíň práce.
  2. **Port Deno → malá Node/Hono API** na Dokploy: přepiš 4 handlery do
     `apps/api`, appka volá `https://api.hummy.<doména>/...`. Čistší, ale přepis.
- **E-mail:** žádný (Pro billing je Stripe checkout na webu). Nic neřešíš.
- **Data:** `searches`, `events`, `rate_limits` (+ `bump_rate_limit()`), `subscribers`,
  `stripe_events` → dump/restore. `subscribers` je klíčované e-mailem (Pro status).
- **App env repoint:** `EXPO_PUBLIC_SUPABASE_URL`, `EXPO_PUBLIC_SUPABASE_ANON_KEY`,
  `EXPO_PUBLIC_SITE_URL`. Edge fns env: ACRCloud, Spotify, Stripe (`supabase/functions/.env`).
- **Vydání:** změna URL v appce = nový build přes EAS (uživatelé musí aktualizovat) —
  nebo drž zpětně kompatibilní URL, ať staré verze appky nepřestanou fungovat.
  ⚠️ Zvaž: existující instalace míří na starý Supabase; udrž ho běžet, dokud
  neaktualizují, nebo nasměruj starou doménu na nový backend.

## explain-and-act (mobilní část, 🔴 kvůli přímému přístupu)

- **Strategie A (doporučeno):** self-hosted Supabase → mobil funguje beze změny
  (repoint `EXPO_PUBLIC_SUPABASE_URL`), protože PostgREST + GoTrue + Storage běží
  dál a RLS ho chrání. Zdaleka nejmíň práce.
- **Strategie B (čistá, ale hodně práce):** musíš do `apps/api` doplnit endpointy
  pro vše, co dnes mobil dělá přímo (list dokumentů, delete dokumentu, delete
  storage objektu) + auth story, a z mobilu odstranit `supabase.from/.storage/.auth`.
  Teprve pak je pravidlo „mobil nesahá na DB" splněno.
- Web část (`apps/web`) a `apps/api` viz `type-nextjs-supabase-fullstack.md`.
- `supabase/verify.sh` v repu dokumentuje přesně, jaké GoTrue/Storage shimy jsou
  potřeba na holém Postgresu — dobrý odrazový bod pro Strategii B.

## Univerzální ověření (mobil)
- [ ] V mobilní appce NENÍ žádný přímý DB connection string
- [ ] Appka volá jen API/edge funkce přes HTTPS (repoint URL funguje)
- [ ] Staré nainstalované verze appky nespadnou (starý backend běží / URL kompatibilní)
- [ ] Entitlement/Pro status a Stripe webhooky fungují proti nové DB
