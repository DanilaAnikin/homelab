# Migrace projektů ze Supabase na vlastní server

## Obecný postup (čistá Postgres data)

```bash
# 1) dump ze Supabase (connection string: Dashboard → Settings → Database,
#    použij "Session pooler" na portu 5432)
pg_dump -Fc --no-owner --no-privileges -n public \
  "postgresql://postgres.<ref>:<heslo>@aws-0-eu-central-1.pooler.supabase.com:5432/postgres" \
  -f projekt.dump

# 2) cílová DB na serveru
ssh anakin@homelab '/srv/homelab/scripts/newdb.sh projekt'

# 3) restore (přes ssh tunel: ssh -L 5432:localhost:5432 anakin@homelab)
pg_restore --no-owner --role=projekt -d "postgres://projekt:HESLO@localhost:5432/projekt" projekt.dump

# 4) v appce přepni DATABASE_URL → hotovo
```

`-n public` = jen tvoje data. Supabase systémová schémata (`auth`, `storage`,
`realtime`) jsou jejich infrastruktura — nemigrují se, nahrazují se (níže).

## Pokud projekt používá supabase-js SDK — co čím nahradit

| Supabase feature | Náhrada na vlastním serveru | Pracnost |
|---|---|---|
| čistá DB (Prisma/Drizzle nad Supabase) | jen přepnout URL | ~0 |
| Auth | **Better Auth** / Auth.js (tabulky ve tvé DB) | hodiny–den |
| Storage | Cloudflare R2 přes S3 SDK | hodiny |
| Realtime | ws v appce (Socket.IO) / polling | dle použití |
| Edge Functions | API routes v appce | malá |
| RLS policies | běžná autorizace v API vrstvě | dle rozsahu |

## Tvoje projekty

- **LeadCRM** — Next.js + Supabase. Kanban/leads data v `public` schématu → dump/restore snadný. Zkontrolovat, jestli používá supabase-js auth, nebo jen DB — podle toho pracnost. Kandidát na první migraci.
- **Hummy** — je ve fázi 0 (validace) → **nemigrovat, rovnou stavět na novém serveru.** Mobilní appka NIKDY přímo na Postgres — API vrstva (Next.js/Hono na Dokploy), ta mluví s DB interně.
- **agent-farm** — návrh počítal se Supabase → v návrhu rovnou nahradit shared Postgresem (`newdb.sh agent_farm`) + Better Auth. Levnější a jednodušší, než bylo v plánu.

## Zásady

1. Migruj **po jednom projektu**, ne vše najednou. Supabase nech běžet jako fallback, dokud si nový domov pár dní neověří.
2. Po každé migraci: smoke test appky + druhý den zkontroluj, že se projekt objevil v záloze (`rclone ls r2:homelab-backups | grep projekt`).
3. Až si věříš: Supabase projekt pauzni (free tier to udělá sám 😄).
