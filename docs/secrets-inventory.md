# Secrets inventory (JEN názvy/konzumenti/zdroj — ŽÁDNÉ hodnoty)

Skutečné hodnoty žijí v `/srv/homelab/secrets/` (server, 0600) a částečně na
workstationu (`~/programming/homelab/secrets/`, gitignored). Off-site: šifrovaný
`secrets` bundle v denní R2 záloze. Tento soubor je jen DR mapa „co existuje a kde to vzít".

## Server `/srv/homelab/secrets/`
| soubor | konzument | zdroj / jak obnovit |
|---|---|---|
| `freio-backup-key.txt` | backup.sh (AES) | **i workstation + Obsidian vault** (3 kopie, SHA musí sedět) |
| `freio-app.env` | freio-app kontejner | Dokploy env + Stripe/Google/Supabase konzole |
| `freio-app-runtime.env` | freio-app runtime | Stripe live keys, STRIPE_EXPECTED_* |
| `freio-cloudflare-tunnel-token.txt` | cloudflared (freio) | Cloudflare Zero Trust → Tunnels |
| `freio-cloudflare-dns-token.txt` | DNS skripty | Cloudflare API tokens |
| `freio-pooler-pw.txt` | freio supabase pooler | supabase.env POSTGRES_PASSWORD |
| `rclone.conf` | backup.sh (R2) | Cloudflare R2 API token (S3) |
| `cloudflared-token.txt` | cloudflared (homelab) | Cloudflare Zero Trust |
| `kuma-login.txt` | poller.py | Uptime Kuma admin |
| `claude-oauth-token.txt` | self-healing agent | `claude` subscription OAuth |
| `dokploy-api-token.txt`* | agent (Dokploy API) | Dokploy → Settings → API |
| `launchmail.env`, `launchmail-*` | launchmail stack | LaunchMail konfigurace |
| `ripieno.env` | ripieno stack | Ripieno konfigurace |
| `uptimerobot.env` | (legacy monitoring) | UptimeRobot |
| `kuma-backup-push-url.txt` | backup.sh | Kuma push monitor „Nightly backup" |

## Server `/etc/homelab-telegram/`

| soubor | konzument | zdroj / jak obnovit |
|---|---|---|
| `telegram-token` | socket transport, Alertmanager, Freio handoff | BotFather; po historické expozici vždy nový rotovaný token |
| `telegram-chat-id` | socket transport, Alertmanager, Freio handoff | cílový Telegram chat; nikdy nevkládat do gitu nebo env |

## Jen workstation (`~/programming/homelab/secrets/`)
`brevo.txt`, `cloudflare-api-token.txt`, `contact-mailboxes.txt`, `grafana-login.txt`,
`inngest.env`, `r2-lokwave.txt`, `wedos-dns-snapshot.txt`, `launchmail-domain-ids.txt`.

## Interní identifikátory (neexploitovatelné, ale mimo git)
- `homelab.conf` — TUNNEL_ID, DOKPLOY_API/ENV_ID/GITHUB_ID. Historické Telegram položky po migraci odstranit.
- `dokploy-apps.conf` (server) — mapování appName→appId pro self-healing agenta.

## Externí (mimo homelab)
- Telegram legacy: `/srv/frem/telegram-token` a hodnoty v `homelab.conf` jsou po
  migraci deprecated; token musí být rotovaný a staré zdroje odstraněné.
- Grafana admin pw + PG exporter DSN: v `compose/observability/.env` (gitignored); v gitu jen `${VAR}` šablona.

\* server vs workstation se historicky rozešly — při DR ber jako zdroj pravdy R2 secrets bundle (nejnovější).
