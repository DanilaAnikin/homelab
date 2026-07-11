# 🏠 Homelab — stavebnice domácího serveru

Kompletní kit pro Acemagic F1A (i9-12900H, 32 GB, 1 TB): Dokploy + všechny weby,
domény, databáze, SMTP a zálohy. Vše self-hosted, žádné měsíční platby.

## Kudy do toho

1. **[PLAN.md](PLAN.md)** — co stavíme a proč (architektura, rozhodnutí, kapacita)
2. **[RUNBOOK.md](RUNBOOK.md)** — krok-za-krokem checklist od vybalení po první projekt

## Mapa složek

| Cesta | Co je uvnitř |
|---|---|
| `iso/` | Ubuntu Server 24.04 installer (stažený + checksum) |
| `scripts/bootstrap.sh` | jednorázový setup čerstvého serveru (hardening, Docker, Dokploy, Tailscale, cloudflared) |
| `scripts/newdb.sh` | „Supabase zážitek": nová DB + user + connection stringy jedním příkazem |
| `scripts/backup.sh` + `scripts/systemd/` | denní zálohy Postgresu → USB SSD + Cloudflare R2 |
| `scripts/smoke-test.sh` | kontrola, že všechno běží, jak má |
| `compose/postgres/` | sdílený PostgreSQL 17 + PgBouncer pro všechny projekty |
| `compose/smtp/` | interní SMTP služba (relay přes Brevo) pro všechny appky |
| `compose/monitoring/` | Uptime Kuma |
| `launchmail/` | **vlastní mail platforma** (nezávislá kopie launchmailu) — self-hosted ESP s direct-MX doručováním (Fáze 1–4 hotové: rate limity, greylist retry, bounce handling, warm-up, deliverability konzole); roadmap: `launchmail/DIRECT_DELIVERY_PLAN.md` |
| `compose/mail-egress/` + `docs/mail-egress-node.md` | **egress node** pro plné vlastní odesílání (host s PTR + port 25); do té doby jede Seznam SMTP smarthost |
| `docs/networking.md` | Cloudflare Tunnel, domény, Tailscale |
| `docs/new-project-recipe.md` | 5min recept: nový projekt od DNS po deploy |
| `docs/backups-restore.md` | R2 + USB setup a hlavně: JAK OBNOVIT |
| `docs/migrate-from-supabase.md` | přesun LeadCRM / Hummy / agent-farm |

> Tip: až to poběží, udělej si z této složky git repo — je to tvoje infrastruktura jako kód.
