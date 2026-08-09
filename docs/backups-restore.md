# Zálohy a obnova

Filozofie: **3-2-1** — data na serveru + lokální kopie (USB SSD) + offsite (R2).
Zálohuje se denně ve 3:30 (`backup.timer`). Netestovaná záloha = žádná záloha →
kvartální restore drill (dole).

## Jednorázové nastavení

### A) USB SSD (lokální kopie)

```bash
lsblk -o NAME,SIZE,MODEL,TRAN            # najdi USB disk, např. sda (TRAN=usb)
sudo mkfs.ext4 -L BACKUP /dev/sdX        # ⚠️ smaže disk — zkontroluj písmeno!
echo 'LABEL=BACKUP /mnt/backup ext4 defaults,nofail,noatime 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a
mountpoint /mnt/backup                   # "is a mountpoint" ✅
```
`nofail` = server nastartuje i s odpojeným diskem (záloha pak jede jen do R2).

### B) Cloudflare R2 (offsite)

✅ Hotovo: primární bucket `homelab-backups` a sekundární DR bucket
`homelab-backups-dr` jsou dostupné přes oddělené rclone remotes `r2` a `r2dr`.
Hotový config je v `secrets/rclone.conf`. Na serveru stačí:
```bash
sudo install -m 600 -D /srv/homelab/secrets/rclone.conf /root/.config/rclone/rclone.conf
sudo rclone lsf r2:homelab-backups --max-depth 1
sudo rclone lsf r2dr:homelab-backups-dr --max-depth 1
```

### C) Hlídání záloh (Uptime Kuma)

Kuma → Add Monitor → **Push**, jméno `backup`, Heartbeat Interval `90000` s.
Zkopíruj Push URL → vlož do `/usr/local/bin/homelab-backup.sh` (`KUMA_PUSH_URL=`).
Když záloha neproběhne, Kuma spustí alert.

### D) Aktivace (viz RUNBOOK fáze 6)

Nainstaluj skript + systemd units, pusť první zálohu ručně, ověř `rclone ls r2:homelab-backups`.

## Co záloha obsahuje

| Soubor | Obsah |
|---|---|
| `globals_*.sql.gz` | role + hesla (nutné pro funkční conn stringy po obnově) |
| `db_<projekt>_*.dump` | každá databáze zvlášť (`pg_dump -Fc`) |
| `etc-dokploy_*.tar.gz` | definice aplikací/domén/env v Dokploy |

**Není v záloze:** soubory uvnitř kontejnerů (uploads patří do R2 přímo z appky),
OS (reprodukovatelný přes `bootstrap.sh`), `data/` adresář Postgresu (zálohou
jsou logické dumpy, ne raw soubory).

## Obnova JEDNÉ databáze (nejčastější případ)

```bash
# vezmi dump z /mnt/backup/homelab/ nebo stáhni z R2:
rclone copy r2:homelab-backups/2026-07/db_hummy_2026-07-15_03-30.dump /tmp/

# obnov (přepíše obsah DB!):
sudo docker exec -i shared-postgres pg_restore -U postgres -d hummy \
  --clean --if-exists --no-owner --role=hummy < /tmp/db_hummy_*.dump
```

## Obnova VŠEHO (disaster: mrtvý server)

Na novém stroji (jakémkoli x86 s Ubuntu):
```bash
# 1) bootstrap.sh + Dokploy + postgres compose (RUNBOOK fáze 2–5, ~30 min)
# 2) stáhni poslední zálohu
rclone copy r2:homelab-backups/2026-07/ /tmp/restore/
# 3) role a hesla
gunzip -c /tmp/restore/globals_*.sql.gz | sudo docker exec -i shared-postgres psql -U postgres
# 4) každý projekt
for f in /tmp/restore/db_*.dump; do
  db=$(basename "$f" | sed -E 's/^db_(.+)_[0-9-]+_[0-9-]+\.dump$/\1/')
  sudo docker exec shared-postgres createdb -U postgres -O "$db" "$db" 2>/dev/null
  sudo docker exec -i shared-postgres pg_restore -U postgres -d "$db" --no-owner --role="$db" < "$f"
done
# 5) /etc/dokploy z tarballu + znovu napojit appky v Dokploy UI,
#    cloudflared service install <TOKEN> — a jedeš. Conn stringy platí beze změny.
```

## Kvartální restore drill (15 min, NEVYNECHÁVAT)

```bash
sudo docker exec shared-postgres createdb -U postgres -O <projekt> drill_test
sudo docker exec -i shared-postgres pg_restore -U postgres -d drill_test --no-owner --role=<projekt> < <poslední_dump>
sudo docker exec shared-postgres psql -U postgres -d drill_test -c "\dt" -c "SELECT count(*) FROM <nejaka_tabulka>;"
sudo docker exec shared-postgres dropdb -U postgres drill_test
```
Prošlo? Zálohy fungují. Zapiš si datum drillu.
