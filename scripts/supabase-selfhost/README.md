# Self-hosted Supabase stack (per-app) — homelab

Generalizovaný Freio pattern pro nasazení izolovaného Supabase stacku per projekt.
Použito pro gorilla-type (gtapi.anikin.cz) a classio (capi.anikin.cz).

## Postup
1. `provision-supabase-stack.sh <app> <api_domain>` — zkopíruje upstream compose,
   namespacuje (compose -p <app>-supabase, strip container_name; kong dostane
   stabilní jméno <app>-supabase-kong na dokploy-network kvůli Traefiku).
2. `gen-supabase-env.py <app> <api_domain> <site_url> > .env` (chmod 600) — generuje
   všechny secrety (HS256 JWT, anon/service keys, atd.).
3. `docker compose -p <app>-supabase --env-file .env -f docker-compose.yml up -d`
4. **Gotcha 1:** interní role (authenticator, supabase_auth_admin, supabase_storage_admin,
   …) nemají po initu heslo = POSTGRES_PASSWORD → nastavit ručně jako superuser
   `supabase_admin`: `ALTER ROLE <role> WITH PASSWORD '<POSTGRES_PASSWORD>'`.
5. **Gotcha 2:** po opravě hesel `docker compose up -d --force-recreate auth rest storage`
   (plain restart nestačí kvůli backoff).
6. **Gotcha 3:** SUPABASE_PUBLISHABLE_KEY / SUPABASE_SECRET_KEY musí být DISTINKTNÍ
   (ne == anon/service), jinak kong.yml keyauth uniqueness violation.
7. Traefik route: /etc/dokploy/traefik/dynamic/<app>.yml → Host(api_domain) → <app>-supabase-kong:8000
8. CF DNS CNAME api_domain + app domain → tunnel (proxied) + tunnel ingress → localhost:80
9. Migrace: `psql -U supabase_admin -d postgres < migrace.sql`
10. App: Dokploy (GitHub, dockerfile build, NEXT_PUBLIC_* jako buildArgs), domain app_domain.
11. Přidat <app>-supabase-db do BACKUP_TARGETS v backup.sh + Kuma monitory.
