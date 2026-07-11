# Recept: nový projekt za ~5 minut

Předpoklad: server běží (RUNBOOK dokončený), doména je na Cloudflare a napojená
na tunel (`docs/networking.md` krok 3 — jednou per doména).

## 1) Databáze (30 s)

```bash
ssh anakin@homelab
/srv/homelab/scripts/newdb.sh mujprojekt
```
→ vypíše `DATABASE_URL` (pooled) a `DIRECT_URL` (migrace). **Ulož do hesláře.**

## 2) Aplikace v Dokploy (2 min)

Dokploy (`http://homelab:3000`) → Project → **Create Service → Application**:
- Source: GitHub repo (poprvé propoj GitHub App) + branch
- Build: **Nixpacks** (autodetekce Next.js/Node) nebo Dockerfile, máš-li vlastní
- **Environment:**
  ```env
  DATABASE_URL=postgres://mujprojekt:HESLO@shared-pgbouncer:6432/mujprojekt
  DIRECT_URL=postgres://mujprojekt:HESLO@shared-postgres:5432/mujprojekt
  SMTP_HOST=smtp
  SMTP_PORT=587
  SMTP_SECURE=false
  EMAIL_FROM=mujprojekt@mail.TVOJE-DOMENA.cz
  ```
- Deploy 🚀 (další deploye: push do branche + auto-deploy webhookem)

## 3) Doména (1 min)

App → Domains → `mujprojekt.domena.cz`, container port (Next.js: 3000), **HTTPS OFF**.
Díky wildcard záznamu netřeba sahat do Cloudflare. Web běží na `https://mujprojekt.domena.cz`.

## 4) Monitoring (1 min)

Kuma (`http://homelab:3001`) → Add Monitor → HTTP(s) → `https://mujprojekt.domena.cz`.
Klíčové projekty přidej i do UptimeRobot (externí pohled — pozná i výpadek celého domu).

---

## Poznámky per stack

**Prisma** — transaction pooling vyžaduje flag + directUrl:
```prisma
datasource db {
  provider  = "postgresql"
  url       = env("DATABASE_URL")   // ...6432/db?pgbouncer=true  ← přidej flag!
  directUrl = env("DIRECT_URL")     // migrace jdou přímo na 5432
}
```

**Drizzle / node-postgres / porty obecně** — pooled URL funguje rovnou; migrace
(`drizzle-kit push`) pouštěj přes `DIRECT_URL`. V aplikaci drž malý pool
(`max: 5`) — o víc se stará PgBouncer.

**Next.js standalone** — v `next.config.js` nastav `output: "standalone"`
(menší image, rychlejší deploy).

**Statické weby** (vizitky, landing pages) — Dokploy Application typu Static
(build command + output dir). RAM ~0, počet neomezený.

**Soubory/uploads** — neukládej na disk kontejneru (deploy je smaže). Použij
Cloudflare R2 (S3 SDK) — stejný účet jako zálohy.

**Cron joby** — Dokploy má Schedules (spustí příkaz v kontejneru), nebo klasický
cron na serveru.
