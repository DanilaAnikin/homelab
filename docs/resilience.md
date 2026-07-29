# Homelab — odolnost (backup + self-healing)

Přehled „nerozbitného" systému nasazeného 2026-07-29 po 4 auditech (backup/DR,
self-healing, boot/zdroje, reprodukovatelnost). Cíl: nic se tiše nerozbije, a co
se rozbít dá, se samo opraví nebo hlasitě upozorní.

## 1. Zálohy (`scripts/backup.sh` → `/usr/local/bin/homelab-backup.sh`)
- `backup.timer` denně 03:30. Zálohuje **šifrovaně** (openssl AES-256, klíč
  `freio-backup-key.txt`) na Cloudflare R2 `r2:homelab-backups/nightly/YYYY-MM/`:
  - **DB**: freio (LIVE), lokwave, inngest, ripieno, launchmail, dokploy(metadata) +
    pg globals každého clusteru. Rehearsal/system DB (`freio_src*`, `_supabase`) se vynechávají.
  - **config bundle**: `/etc/dokploy` + `/srv/homelab/{compose,self-healing,email-bot}` + systemd unity.
  - **secrets bundle**: `/srv/homelab/secrets` (nejcitlivější, šifrované).
- **Klíč mimo stroj** (jinak jsou šifrované zálohy po ztrátě disku k ničemu):
  `freio-backup-key.txt` je i na workstationu (`~/programming/homelab/secrets/`, gitignored)
  a v Obsidian vaultu (`Projects/homelab/secrets/`). SHA všech 3 se musí shodovat.
- **Fail-loud**: skript vrací exit 1 při jakémkoli dílčím selhání →
  `OnFailure=backup-notify-failure.service` → Telegram. Úspěch pinguje Kuma push
  monitor „Nightly backup" (interval 25h) → když ping nedorazí (skript vůbec neběžel),
  monitor jde DOWN → poller → agent.
- **Retence**: `rclone delete r2:homelab-backups/nightly --min-age 30d` — scoped JEN na
  `nightly/`, nikdy nemaže `freio-migration/` ani `ripieno/` point-in-time captures.
- **Restore**: stáhni `.enc`, `openssl enc -d -aes-256-cbc -pbkdf2 -in X.enc -out X -pass file:freio-backup-key.txt`,
  pak `pg_restore`. Ověřeno 2026-07-29 (freio dump = 1559 objektů, 97 tabulek, restorovatelný).

## 2. Reaktivní self-healing (`self-healing/poller.py` + `respond.sh`)
`self-healing.service` (User=anakin, Restart=always) polluje každých 90s **dva zdroje**:
- **Uptime Kuma** DOWN monitory (web/DB/infra dostupnost).
- **Prometheus** FIRING alerty (přes `docker exec obs-prometheus`, není publikovaný na host).
  Actionable třídy → agent: `DiskDochazi, KontejnerSeRestartuje, PostgresNedostupny,
  MaloPameti, KontejnerZeraPamet, PostgresDochazejiSpojeni, CertifikatBrzyVyprsi`.

Incident (dedup 30 min) → `respond.sh` → `claude -p` headless (Bash, NOPASSWD sudo)
dle železných pravidel v `self-healing/CLAUDE.md` (nikdy nemazat data/volumes/DNS/secrets,
preferuj nejmenší zásah, při nejistotě ESKALACE). **Každý výsledek** (OPRAVENO / ESKALACE /
timeout) jde na **Telegram** (dřív končily eskalace tiše jen v `incidents.log`).

## 2b. Restore drill (`self-healing/restore-drill.sh`, `restore-drill.timer` So 05:00)
„Netestovaný dump není záloha." Týdně: stáhne nejnovější `db_{freio,ripieno,lokwave}`
.enc z R2, dešifruje, obnoví do **izolovaného throwaway** `supabase/postgres:17.6.1.136`
kontejneru (`pg-restore-drill`, žádná prod síť/volume), ověří schema (počet tabulek) +
data (nejlidnatější tabulka > 0 řádků), uklidí, hlásí na Telegram. Ověřeno 2026-07-29:
freio 62 tab / test_questions 63600, ripieno 33 / events 3312, lokwave 38 / outreach_prospects 670.

## 2c. Watchdog-on-watchdog
Poller pushuje heartbeat do Kuma push monitoru „self-healing alive" každý cyklus (90s).
Když poller **tiše zamrzne** (ne jen spadne — to řeší `Restart=always`), monitor jde DOWN
a Kuma **SÁM** pošle Telegram přes vlastní notifikaci „Telegram-owner" (nezávisle na polleru,
který se neumí hlídat sám). URL v `kuma-selfheal-push-url.txt`.

## 3. Proaktivní health-review (`self-healing/daily-health-review.sh`)
`daily-health-review.timer` denně 07:00 → LLM agent projde disk, kontejnery (unhealthy/
restart county), certy (freio/ripieno/lokwave), **stáří poslední R2 zálohy**, firing alerty,
pg spojení. Bezpečně opraví jen prune (>80 % disk) a restart čistě spadlého kontejneru,
zbytek jen nahlásí. Pošle **Telegram digest**. Chytá pomalé problémy dřív, než z nich je incident.

## 4. Guardrails / SPOF
- **Cloudflare tunnel** (`cloudflared`, SPOF pro veřejný HTTPS): `OnFailure` → Telegram +
  runbook v CLAUDE.md (agent umí `systemctl restart cloudflared`). Když padne víc webů
  najednou a kontejnery jsou healthy → podezření na tunel.
- **docker-image-prune.timer** týdně (Ne 04:30) — disk hygiena, BEZ `--volumes`.
- **freio.cz + www.freio.cz** přidány do Prometheus blackbox (cert + uptime).
- Memory limity kontejnerů **záměrně nenasazeny** (22Gi volných, riziko OOM > přínos);
  paměťové problémy detekují alerty `MaloPameti`/`KontejnerZeraPamet` → agent.

## 5. Známé follow-upy
- `compose/observability/docker-compose.yml` má inline secrety (Grafana pw, PG exporter DSN)
  → před commitem sanitizovat na `${VAR}`; zatím jen v šifrovaném config bundlu na R2.
- Restore drill (týdenní automatická verifikace dumpů do throwaway DB) — zatím ruční.
- Secrets inventory viz `docs/secrets-inventory.md`.
