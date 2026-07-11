# Master plán: domácí server pro ~100 projektů

**Hardware:** Acemagic F1A — i9-12900H (14c/20t), 32 GB DDR4, 1 TB NVMe
**OS:** Ubuntu Server 24.04 LTS (nudné = spolehlivé; podpora do 2029)
**Cíl:** všechny weby, domény, databáze a odchozí e-maily vlastních projektů na jednom stroji doma, bez měsíčních plateb, s pořádnými zálohami.

---

## Principy

1. **Jedno kontrolní místo.** Aplikace se nasazují přes Dokploy (UI, git deploy, logy). Infrastruktura (Postgres, SMTP, monitoring) běží jako obyčejný `docker compose` v `/srv/homelab` — verzovatelná, nezávislá na Dokploy.
2. **Žádné otevřené porty do internetu.** Domácí net má CGNAT a stejně nechceme nic forwardovat. Veškerý veřejný provoz jde **Cloudflare Tunnelem** (odchozí spojení ze serveru). Admin přístup (SSH, panely) jde **jen přes Tailscale**.
3. **Data jsou posvátná, hardware je spotřebák.** Denní zálohy 3-2-1 (server + USB SSD + Cloudflare R2). Kdyby no-name krabička umřela, obnovíme se na čemkoli za ~hodinu. Restore se pravidelně zkouší.
4. **Jeden sdílený Postgres, ne 100 kontejnerů.** Každý projekt = vlastní databáze + vlastní user (izolace), ale jedna instance (úspora RAM, jedna věc na údržbu). Přes PgBouncer — stejný model jako Supabase (pooled + direct string).
5. **Opakovatelnost.** Nový projekt = 5minutový recept. Nová instalace serveru = jeden skript.

---

## Architektura

```
                    INTERNET
                       │
        ┌──────────────┴──────────────┐
        │         CLOUDFLARE          │
        │  DNS (všechny domény)       │
        │  Tunnel (příchozí provoz)   │──── Email Routing (příjem pošty → Gmail)
        │  R2 (offsite zálohy)        │
        └──────────────┬──────────────┘
                       │ pouze ODCHOZÍ spojení (žádný otevřený port)
   ~~~~~~~~~~~~~~~~~~~ │ ~~~~~~~~~~~~~~ domácí router (CGNAT, nic neforwarduje)
                       │
   ┌───────────────────┴────────────────────────────────────┐
   │  SERVER (Ubuntu 24.04)                                  │
   │                                                         │
   │  cloudflared ──► Traefik :80 (součást Dokploy)          │
   │                     │ routing podle Host hlavičky       │
   │       ┌─────────────┼──────────────┬─────────────┐      │
   │       ▼             ▼              ▼             ▼      │
   │   projekt A     projekt B      projekt C     …až ~100   │
   │   (Dokploy apps: Next.js, API, statické weby)           │
   │       │             │              │                    │
   │       ▼             ▼              ▼                    │
   │  ┌────────────────────────────┐  ┌───────────────┐      │
   │  │ shared-pgbouncer :6432     │  │ smtp :587     │      │
   │  │ shared-postgres  :5432     │  │ (Postfix      │      │
   │  │ (1 instance, ~100 databází)│  │  → relay ven) │      │
   │  └────────────────────────────┘  └───────────────┘      │
   │                                                         │
   │  Uptime Kuma :3001 │ Dokploy panel :3000 │ tailscaled   │
   │  systemd timer: backup.sh → USB SSD + R2 (denně 3:30)   │
   └─────────────────────────────────────────────────────────┘
        admin přístup: Tailscale (SSH, :3000, :3001)
```

## Komponenty a rozpočet RAM (32 GB)

| Komponenta | RAM | Poznámka |
|---|---|---|
| OS + systémové služby | ~1 GB | |
| Dokploy stack (panel, Traefik, interní DB, Redis) | ~1,5 GB | |
| **shared-postgres** (PG 17) | ~5 GB | `shared_buffers=4GB`, utáhne stovky DB |
| PgBouncer, SMTP, cloudflared, tailscaled, Kuma | ~0,5 GB | |
| **Zbývá na aplikace** | **~24 GB** | malá Node/Next appka ≈ 150–300 MB |

→ **~50–80 dynamických aplikací současně v pohodě**, statické weby prakticky zdarma (servíruje je Traefik/nginx, RAM ~0). CPU (14 jader) se u hobby trafficu bude flákat. Realistický strop dřív narazí na **upload domácí linky** než na železo — statiku cachuje Cloudflare, takže běžné weby OK; jen velké soubory/média servíruj z R2.

## Databáze — „vlastní Supabase"

- PostgreSQL 17 + PgBouncer (transaction pooling), oba v `dokploy-network`.
- Nový projekt: `newdb.sh nazev` → vytvoří DB + user + heslo a vypíše:
  - `DATABASE_URL` … přes PgBouncer :6432 (běžný provoz aplikace)
  - `DIRECT_URL` … přímo :5432 (migrace — prisma migrate, drizzle push)
- Izolace: user vidí jen svou DB (`REVOKE CONNECT … FROM PUBLIC`).
- Admin z notebooku: `ssh -L 5432:localhost:5432 homelab` → TablePlus/psql na localhost.
- `pg_stat_statements` zapnuté — kdykoli zjistíš, který projekt žere výkon.
- Redis/klíč-hodnota: až bude potřeba, jeden sdílený `redis` kontejner stejným vzorem.

## E-maily — vlastní launchmail, žádná třetí strana

**Mailová vrstva = vlastní instance launchmailu** (`launchmail/` v tomto repu — samostatná kopie, produkce LaunchDay se nedotýká). Self-hosted ESP platforma: API, worker s frontami, šablony, tracking, DKIM podpisy, suppressions, IMAP příjem, web UI. Všech ~100 projektů posílá přes launchmail API/SDK. Vlastní postgres16+redis (compose připravený pro dokploy-network), UI na `mail.<doména>` přes tunnel.

**Doručování do světa — 100 % vlastní pipeline** (plán: `launchmail/DIRECT_DELIVERY_PLAN.md`):
- Fáze P1–P4: direct-MX transport (resolveMx → SMTP :25 + STARTTLS + DKIM), per-domain rate limity, bounce/DSN parsing přes existující IMAP sync, deliverability konzole.
- Fáze P5: **delivery node** — mini VPS (~€4/měs, Hetzner CAX11/CX22) s vlastní **PTR** a odemčeným portem 25 (⚠️ unlock na ticket, objednat s předstihem). Na nodu běží jen náš `worker` (`WORKER_ROLE=direct`) připojený k domácí Redis frontě přes Tailscale. Pronajímá se jen hloupá IP — software, data, reputace: všechno naše.
- Proč node: změřeno 2026-07-11 — domácí IP 185.120.71.202 má port 25 otevřený, ale **bez PTR** → Gmail/Outlook/Seznam přímé maily odmítají. PTR na domácí lince nezískáš; i komerční ESP si IP pronajímají.
- Do zprovoznění node: appky maily do světa neposílají (hobby fáze — OK), nouzovka = dočasný SmtpConfig na vlastní Gmail (smtp.gmail.com:587, app password, 500/den, žádná nová služba).

**DNS odesílací domény** (až s nodem): SPF `ip4:<node-ip>`, DKIM klíč z launchmailu, DMARC, PTR = `mail.<doména>`. **Příjem pošty:** Cloudflare Email Routing (zdarma) → forward do Gmailu; plné schránky později přes launchmail IMAP sync. Postfix z `compose/smtp/` je deprecated — nechán jen jako nouzový fallback.

## Zálohy (3-2-1) — nejdůležitější část celého plánu

| Co | Kam | Kdy | Retence |
|---|---|---|---|
| pg_dump každé DB zvlášť (`-Fc`) + globals (role/hesla) | USB SSD (`/mnt/backup`) | denně 3:30 | 14 dní |
| totéž | Cloudflare R2 (free 10 GB, egress zdarma) | denně 3:30 | 30 dní |
| `/etc/dokploy` (konfigurace deploymentů) | obojí | denně | dtto |

- Per-DB dumpy → obnovíš **jeden projekt** bez sahání na ostatní.
- Selhání zálohy hlásí Uptime Kuma (push monitor — když ping nedorazí, alert).
- **Restore drill každý kvartál** (postup v `docs/backups-restore.md`). Netestovaná záloha = žádná záloha.
- Bootstrap skript = reprodukovatelný OS → neálohujeme systém, jen data.

## Bezpečnost (vrstvy)

1. Router: nic neforwarduje (CGNAT) — z internetu není vidět vůbec nic.
2. Veřejný provoz: jen Cloudflare Tunnel → Traefik (HTTPS terminuje CF edge).
3. Admin (SSH, Dokploy :3000, Kuma :3001): **jen Tailscale**; panel NIKDY nepublikovat tunelem.
4. SSH: jen klíče, root login vypnutý, fail2ban.
5. OS: unattended-upgrades (security), Docker log-rotace, UFW (pozn.: docker porty UFW obcházejí — reálná ochrana je vrstva 1+2).
6. Sekrety: `.env` soubory na serveru (chmod 600) + heslář (Bitwarden/Vaultwarden později).

## Monitoring

- **Uptime Kuma** (na serveru): HTTP monitory webů, TCP monitory postgres/smtp, push monitor záloh.
- **UptimeRobot** (externí, free): 2–3 klíčové weby — pokryje scénář „celý dům offline", který Kuma z principu nevidí.
- **Dokploy**: metriky kontejnerů, logy, restarty v UI.

## Fáze (mapují na RUNBOOK.md)

| # | Fáze | Kdy |
|---|---|---|
| 0 | Účty (Cloudflare, Brevo, Tailscale, UptimeRobot), doména na CF, USB installer | **teď, před příchodem HW** |
| 1 | Ubuntu instalace + BIOS (auto power-on) | den D |
| 2 | `bootstrap.sh` (hardening, Docker, Dokploy, Tailscale, cloudflared) | den D, ~15 min |
| 3 | Tailscale auth + Dokploy admin účet | den D, ~10 min |
| 4 | Cloudflare Tunnel + první doména | den D, ~15 min |
| 5 | Infra: postgres + smtp + monitoring (`docker compose up -d`) | den D, ~20 min |
| 6 | Zálohy: USB + R2 + timer + první záloha + Kuma | den D, ~30 min |
| 7 | `smoke-test.sh` → vše zelené | den D, 2 min |
| 8 | První projekt end-to-end; pak postupná migrace (LeadCRM, Hummy, …) | dny poté |

## Rizika a mitigace

| Riziko | Dopad | Mitigace |
|---|---|---|
| Smrt no-name hardwaru | výpadek | zálohy + restore drill; obnova na cokoli ~1 h; Hetzner jako nouzový plán B |
| Výpadek proudu/netu doma | weby down | BIOS auto power-on, `Restart=always` kontejnery; hobby projekty výpadek přežijí; UptimeRobot dá vědět |
| Smrt SSD | ztráta dat | denní R2 + USB; smartmontools hlídá zdraví disku |
| Zaplněný disk (logy, images) | vše stojí | Docker log-rotace, Dokploy cleanup, journald strop, týdenní pohled na `df` |
| Únik DB přístupu | únik dat jednoho projektu | DB port nikdy veřejně; izolace per-user; sekrety v .env |
| Přerostl jsi domácí linku | pomalé weby | statika na CF/R2; přesun na VPS = restore drill (~den práce) |
