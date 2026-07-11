# Přesun hostingu: Vercel / Railway → náš Dokploy

Platí pro **všechny web/API/worker projekty**. Cíl: appka běží na našem serveru
přes Dokploy, doména teče přes Cloudflare Tunnel. Frontend statika se dá nechat i
na Vercelu, ale záměr je „všechno u nás".

> Předpoklad: server běží, Dokploy nainstalovaný, Cloudflare Tunnel + doména
> hotové (viz hlavní `RUNBOOK.md` fáze 3–4, `docs/networking.md`).

---

## Rozhodnutí: co kam

| Typ služby | Kam na Dokploy |
|-----------|----------------|
| Next.js appka (SSR/API) | **Application** (Nixpacks nebo Dockerfile) |
| Standalone Node worker (claude-trader, ripieno engine) | **Application** (bez domény, jen běží) |
| Python backend/agent | **Application** (Nixpacks python / Dockerfile) |
| Cron/naplánované úlohy | Dokploy **Schedules** |
| Databáze | **už běží** — sdílený Postgres (`newdb.sh`) |

## 1) Připoj GitHub (jednou)

Dokploy → Settings → Git → **GitHub App** → autorizuj (repo mohou být privátní:
`DanilaAnikin/*`, `LaunchDay-cz/*`).

## 2) Vytvoř aplikaci

Dokploy → Project → **Create Service → Application**:
- **Source:** GitHub repo + branch (`main`)
- **Build type:**
  - **Nixpacks** — autodetekce Next.js/Node/Python. Nejrychlejší start.
  - **Dockerfile** — když repo má vlastní (leadcrm, ripieno engine). Preferuj, pokud existuje (deterministické buildy).
- **Monorepo:** nastav „Build Path" na podadresář (např. `apps/web`), pokud je to Turborepo.

## 3) Environment proměnné

Zkopíruj z `.env.example`, vyplň ostrými hodnotami. Klíčové náhrady při migraci:

```env
# DB → náš sdílený Postgres (z newdb.sh)
DATABASE_URL=postgres://projekt:HESLO@shared-pgbouncer:6432/projekt     # pooled
DIRECT_URL=postgres://projekt:HESLO@shared-postgres:5432/projekt        # migrace

# e-mail → LaunchMail (viz email-to-launchmail.md)
LAUNCHMAIL_URL=https://mail.tvojedomena.cz
LAUNCHMAIL_API_KEY=lm_...

# storage → R2 (viz storage-to-r2.md), pokud projekt nahrává soubory
R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=...
```
Odstraň `SUPABASE_*`, `RESEND_API_KEY` atd. až po dokončení příslušných migrací.

⚠️ **Přenes „neviditelné" klíče**, na kterých závisí dešifrování dat:
`ENCRYPTION_KEY` (claude-trader), `RIPIENO_LOCAL_KEK` (ripieno), `AUTH_SECRET`
(leadcrm), `MAIL_ENCRYPTION_KEY` (launchmail). Bez nich jsou zašifrovaná data
nenávratně ztracená.

## 4) Doména + tunnel

App → **Domains → Add**:
- Host: `projekt.tvojedomena.cz`, Container Port: podle appky (Next.js `3000`), **HTTPS OFF** (TLS řeší Cloudflare).
- Díky wildcard CNAME (`*.tvojedomena.cz` → tunnel) není potřeba sahat do Cloudflare. Detail v `docs/networking.md`.

## 5) Build & deploy

- Deploy 🚀. Sleduj build log.
- **Next.js:** do `next.config.js` přidej `output: "standalone"` (menší image, rychlejší). Dokploy Nixpacks to zvládne i bez, ale standalone je lepší.
- **Migrace DB při startu:** pokud projekt migruje (Prisma/Drizzle/vlastní runner),
  spusť je jako součást startu (Dokploy „Run Command" / v Dockerfile CMD) proti
  `DIRECT_URL`, ne pooleru.
- Další deploye: push do branche → auto-deploy (webhook).

## 6) Ověření (per projekt)

- [ ] Web/API odpovídá na `https://projekt.tvojedomena.cz`
- [ ] Klíčový flow funguje (login, načtení dat, odeslání mailu)
- [ ] Logy v Dokploy bez chyb; DB spojení jde přes PgBouncer
- [ ] Projekt je v noční R2 záloze (`rclone ls r2:homelab-backups | grep projekt`)

## Poznámky per stack

- **Next.js standalone** — nezapomeň na `output: "standalone"`; statické assety
  servíruje Traefik. Cloudflare cachuje statiku, takže domácí upload neřeší běžné weby.
- **Monorepo (Turborepo)** — jeden repo, víc Dokploy Application služeb s různým
  Build Path (`apps/web`, `apps/engine`). Sdílený Postgres, oddělené domény.
- **Worker bez UI** (claude-trader worker, ripieno engine) — Application bez
  domény; komunikace s appkou přes interní Docker síť (`http://engine:PORT`) nebo
  přes frontu (Redis).
- **Python** — Nixpacks detekuje `requirements.txt`/`pyproject.toml`; pro jistotu
  přidej `Procfile` nebo Dockerfile s explicitním start příkazem.
- **Cron** — Vercel Cron / Railway cron → Dokploy Schedules (spustí příkaz v
  kontejneru dle cronu) nebo systémový cron na serveru.
- **Uploady souborů** — NIKDY na disk kontejneru (deploy je smaže) → R2.

## Statika na Vercelu (volitelný hybrid)

Pokud chceš čistě statický frontend nechat na Vercelu (rychlé CDN) a jen backend/
DB dát k nám: frontend volá `https://api.projekt.tvojedomena.cz` (na Dokploy).
CORS povol na API. Ale záměr „vše u nás" = celé na Dokploy.
