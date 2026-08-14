# Resilience & Autonomy — architecture in depth

How this homelab keeps itself alive: nothing breaks silently, and what *can* break either
self-heals or shouts loudly (Telegram). Everything here is live, scheduled by systemd
timers, and version-controlled in this repo.

---

## 1. Backups

### Nightly full backup — `scripts/backup.sh` (`backup.timer`, 03:30)
Encrypted (OpenSSL AES-256) to Cloudflare R2 under `nightly/YYYY-MM/`:
- **Databases** — every production DB dumped individually (`pg_dump -Fc`): freio, lokwave,
  inngest, ripieno, launchmail, **Postiz**, dokploy metadata, plus each cluster's role globals.
  Rehearsal/system DBs are excluded. Dump filenames are namespaced per container so
  same-named DBs (e.g. two `postgres`) never collide.
- **Config bundle** — `/etc/dokploy` + `compose/` + `self-healing/` + systemd units.
- **Secrets bundle** — `/srv/homelab/secrets`, encrypted.
- **Postiz recovery set** — one bounded writer fence captures a WAL-consistent PG17
  cluster, globals + four strict logical DB fallbacks, Redis, runtime/config volume,
  uploads, seasonal rollback state and all four exact Docker images. CAS objects and
  timestamped sets are independently copied to primary and DR under dynamically attested
  Bucket Locks; `--immutable` is only a client collision guard. The HMAC commit is last.
- **Secondary DR copy** — mirrored through the independent `r2dr` remote to the
  separate `homelab-backups-dr` bucket (90-day retention). The copy is followed by
  a one-way size verification. Copy, verification, or retention failure makes the
  whole unit fail, pushes Kuma DOWN, and triggers Telegram via `OnFailure=`; the
  successfully uploaded primary copy is never removed.

### Frequent DB snapshots — `scripts/frequent-db-backup.sh` (`frequent-db-backup.timer`, every 10 min)
Production DBs, including the exact four Postiz/Temporal application DBs, are encrypted to
primary R2 `frequent/`. These are independent per-DB PIT aids, not a cross-DB Postiz
snapshot and not a 10-minute full-service RPO. Complete heartbeat staleness alerts at 45 min.

### Off-box key
The AES key (`freio-backup-key.txt`) lives on the server **and** on the workstation
(gitignored) **and** in an Obsidian vault — three failure domains, matching SHA-256.
Without this, encrypted off-site backups would be undecryptable after disk loss.

### Fail-loud
`backup.sh` exits non-zero on any partial failure → `OnFailure=` → Telegram. Success pings
an Uptime Kuma push monitor (25 h window) — so even a *no-run* surfaces as DOWN → agent.

### Tested recovery — `self-healing/restore-drill.sh` (`restore-drill.timer`, Sat 05:00)
Weekly: pulls the latest encrypted dumps, decrypts, restores into an **isolated throwaway**
Postgres container, asserts schema + data, cleans up, and reports to Telegram. All database
drill containers use `--network none`. The Postiz leg additionally restores its committed
DB/config/uploads/image set independently from primary and DR, verifies every upload hash,
uses strict `pg_restore --exit-on-error`, and validates files in a read-only network-none
container. See [`postiz-backup-restore.md`](postiz-backup-restore.md).

---

## 2. Reactive self-healing — `self-healing/poller.py` + `respond.sh`

`self-healing.service` (Restart=always) polls **three signal sources** every 90 s:

1. **Uptime Kuma** — DOWN monitors (web / DB / infra reachability).
2. **Prometheus** — FIRING alerts (disk, container crashloop, Postgres down, memory,
   connection exhaustion, cert expiry) — read via `docker exec obs-prometheus` (not host-exposed).
3. **Loki** — error-log **spikes** (panic / fatal / OOM / 5xx / "too many connections"),
   detected against an EWMA baseline so steady-state log noise never triggers.

An incident (deduped, 30-min cooldown) → `respond.sh` → **Claude Code headless** diagnoses
and remediates within the iron-clad guardrails in `self-healing/CLAUDE.md` (never touch
data / volumes / DNS / secrets; smallest intervention first; escalate when unsure). It must
**verify** the fix before reporting success; every outcome (FIXED / ESCALATION / timeout)
goes to Telegram.

- **Verify-and-escalate** — if the same incident recurs 3× within a window (the agent claimed
  FIXED but it didn't hold), it stops retrying, backs off, and escalates to a human.
- **Watchdog-on-watchdog** — the poller heartbeats a Kuma push monitor each cycle. If the
  poller *silently freezes* (not just crashes — that's covered by Restart=always), the monitor
  goes DOWN and Kuma alerts independently.

---

## 3. Proactive & self-improving

### Daily health review — `daily-health-review.sh` (07:00)
LLM agent checks disk trend, unhealthy/high-restart containers, cert days-remaining, **last
backup age**, firing alerts, Postgres connections. Auto-fixes only trivial classes (prune,
restart a cleanly-exited container); everything else → Telegram digest. Catches slow-burn
issues before they become 2 a.m. incidents.

### Weekly self-improvement meta-agent — `self-improve.sh` (Sun 08:00)
The "brain": reviews incident history + metrics + config drift (server vs git), writes
**postmortems** with root causes, **applies safe preventive fixes**, and proposes the rest —
then reports. On its first run it found and fixed an **11-hour silent crashloop** (two realtime
containers, a role-password mismatch after a redeploy) that uptime monitoring had missed.

### Synthetic checks — `synthetic-check.sh` (every 10 min)
Deeper than uptime: authenticated `apikey` health (proves GoTrue + Kong + DB are alive, not
just that Kong is up), a real PostgREST query, and page-render keyword checks — each retried
to absorb transient blips.

---

## 4. Security & safe updates

| Component | Behaviour |
|---|---|
| **Trivy** (`trivy-scan.sh`, Mon 06:00) | Weekly CVE scan of running images → Telegram if CRITICAL |
| **Auto-update** (`auto-update.sh`, Sun 05:30) | Observability stack only: pull → **health-gate → rollback** on failure. Pinned prod images (Supabase/Kong/Postgres) are never auto-bumped; app images rebuild via git push |
| **Deploy watchdog** (`deploy-watchdog.sh`, every 3 min) | Post-deploy health gate: a freshly-deployed swarm service that fails → `docker service rollback` to the last working spec (2 h anti-loop) |
| **Cloudflare tunnel** | `OnFailure` → Telegram + a runbook the agent can act on; a SPOF for all public HTTPS |
| **Baseline hardening** | fail2ban, unattended OS security upgrades, weekly image prune (no volumes), zero open inbound ports |

---

## 5. Schedule at a glance

| When | Job |
|---|---|
| every 90 s | self-healing poller (Kuma + Prometheus + Loki) |
| every 3 min | deploy watchdog (auto-rollback) |
| every 10 min | frequent DB snapshots · synthetic checks |
| daily 03:30 | full encrypted backup (+ DR bucket) |
| daily 07:00 | health-review agent |
| Sat 05:00 | restore drill |
| Sun 05:30 | safe auto-update |
| Sun 08:00 | self-improvement meta-agent |
| Mon 06:00 | Trivy CVE scan |
| Sun 04:30 | docker image prune |

---

## 6. Deliberate non-choices

- **Container memory limits** — intentionally not set (plenty of free RAM; risk of spurious
  OOM-kills outweighs the benefit). Memory pressure is caught by Prometheus alerts → agent.
- **Continuous WAL archiving** — not currently enabled. Ten-minute logical dumps remain
  useful per-DB PIT aids, while the nightly Postiz full-recovery authority is the
  writer-fenced physical cluster. Do not describe the dump cadence as a service-wide RPO.
