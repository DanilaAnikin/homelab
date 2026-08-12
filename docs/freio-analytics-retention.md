# Freio first-party analytics retention

`analytics_events` is a raw operational table, not the PostHog dataset. The
database migration `20260812101000_analytics_events_bounded_retention.sql`
removes the retired browser identifiers, enforces their columns as `NULL`, and
exposes a service-role-only oldest-first purge RPC with a fixed 30-day cutoff.

The local job uses `docker exec` and PostgreSQL over the container-local socket.
It reads no application, PostHog, backup, or network credentials and makes no
outbound request. Every run reuses the cutoff returned by its first RPC call,
loops 1000-row batches until `has_more=false`, and fails visibly after 60
batches or five consecutive locked/non-progressing batches. Its state contains
only cutoff, aggregate deleted count, and batch count.

Each local `psql` session executes `SET statement_timeout = '10s'` before its
purge RPC call. This is the effective per-call database guard; the systemd
service keeps a separate 15-minute outer cap for the complete multi-batch
drain.

## Schedule and backup boundary

The production `backup.timer` was verified on 2026-08-12 as enabled and active
at 03:30 with up to 15 minutes randomized delay and `Persistent=true`.
`freio-analytics-retention.timer` therefore runs at 03:00, is itself
`Persistent=true`, and its service declares `Before=backup.service`. With the
daily schedule healthy, expired rows are drained before the nightly dump and
live raw rows stay below 31 days old.

Encrypted backup tails remain separate from the live table: primary R2 nightly
objects are retained 30 days and the independent DR R2 bucket is retained 90
days. Do not use a restored database for traffic until the current migration is
present and `freio-analytics-retention.service` succeeds; this re-applies the
30-day live cutoff before exposure.

## Install and activate

Do this only after the Freio application migration is successfully applied.
Install the retention executable and units first, then install the reviewed
backup script. This order ensures the first new config bundle can contain all
three retention artifacts.

```bash
sudo install -o root -g root -m 0755 \
  /srv/homelab/scripts/freio-analytics-retention.sh \
  /usr/local/sbin/freio-analytics-retention
sudo install -o root -g root -m 0644 \
  /srv/homelab/scripts/systemd/freio-analytics-retention.service \
  /srv/homelab/scripts/systemd/freio-analytics-retention.timer \
  /etc/systemd/system/
sudo install -o root -g root -m 0755 \
  /srv/homelab/scripts/backup.sh \
  /usr/local/bin/homelab-backup.sh
sudo systemctl daemon-reload
sudo systemctl start freio-analytics-retention.service
sudo systemctl status freio-analytics-retention.service --no-pager
sudo cat /var/lib/freio-analytics-retention/last-success.json
sudo systemctl enable --now freio-analytics-retention.timer
systemctl list-timers freio-analytics-retention.timer backup.timer --all --no-pager
sudo systemctl start backup.service
sudo systemctl status backup.service --no-pager
```

Acceptance requires a successful manual drain with `has_more:false`, a root
owned mode-0600 `last-success.json`, and the retention timer scheduled before
the backup timer. The manual backup must finish successfully before relying on
the schedule. The migration RPC must not be called from outreach or another
business workflow.

Verify the newest primary-R2 config bundle contains the installed executable
and both units. This uses the key file directly as an OpenSSL password source;
it does not print or copy the key contents.

```bash
(
set -euo pipefail
verify_dir=$(sudo mktemp -d /var/tmp/freio-retention-config.XXXXXX)
trap 'sudo rm -rf -- "$verify_dir"' EXIT
latest_config=$(sudo rclone \
  --config /srv/homelab/secrets/rclone.conf \
  lsf r2:homelab-backups/nightly \
  --recursive --files-only --include 'config_*.tar.gz.enc' --format p \
  | sort | tail -n 1)
test -n "$latest_config"
sudo rclone --config /srv/homelab/secrets/rclone.conf copyto \
  "r2:homelab-backups/nightly/$latest_config" "$verify_dir/config.tar.gz.enc"
sudo openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$verify_dir/config.tar.gz.enc" \
  -out "$verify_dir/config.tar.gz" \
  -pass file:/srv/homelab/secrets/freio-backup-key.txt
sudo tar -tzf "$verify_dir/config.tar.gz" \
  | sudo tee "$verify_dir/contents.txt" >/dev/null
sudo grep -Fx 'usr/local/sbin/freio-analytics-retention' "$verify_dir/contents.txt"
sudo grep -Fx 'etc/systemd/system/freio-analytics-retention.service' "$verify_dir/contents.txt"
sudo grep -Fx 'etc/systemd/system/freio-analytics-retention.timer' "$verify_dir/contents.txt"
)
```

All three `grep` commands must return one exact path. The patched backup script
marks any config tar or config encryption failure as a failed run (`FAIL=1`),
so systemd/Kuma/OnFailure cannot report a partial config bundle as healthy. A
missing path, config tar failure, encryption failure, backup failure, or decrypt/list failure blocks
activation acceptance and must be investigated before the nightly schedule is
trusted.

## Operational rollback

The migration cutover and every completed purge delete raw rows irreversibly.
Operational rollback therefore disables future scheduled purges; it must not
downgrade the database privacy contract or claim that deleted rows can be
restored from the live database.

```bash
sudo systemctl disable --now freio-analytics-retention.timer
sudo systemctl stop freio-analytics-retention.service
sudo systemctl reset-failed freio-analytics-retention.service
systemctl is-enabled freio-analytics-retention.timer || true
systemctl is-active freio-analytics-retention.timer || true
```

Keep the installed executable and both unit files while the patched backup
script is active: the encrypted config bundle intentionally requires all three
paths. Removing those artifacts first would make every backup fail closed. If
the retention package must be uninstalled, deploy a reviewed previous backup
script before removing the artifacts, then run and verify a manual encrypted
backup. Leave the database migration, CHECK constraint, scrub trigger, RPC
ACLs, and aggregate state intact. After fixing the failure, repeat the manual
drain and backup acceptance sequence before re-enabling the timer.

## Failure and recovery

Any invalid RPC result, database/container failure, non-progressing lock, or
safety-cap backlog exits non-zero. The service uses the existing
`notify-failure@freio-analytics-retention.service` integration. Inspect only
aggregate state and journal output:

```bash
sudo journalctl -u freio-analytics-retention.service -n 100 --no-pager
sudo systemctl start freio-analytics-retention.service
sudo systemctl reset-failed freio-analytics-retention.service
```

If `safety_cap_backlog_remaining` repeats, leave the timer enabled, investigate
write volume/index usage, and run additional individual service starts. Do not
increase the cutoff or bypass the function ACL. If
`locked_or_nonprogressing_backlog` repeats, identify the database transaction
holding `analytics_events` rows before retrying; never disable the CHECK or
scrubbing trigger.
