# Postiz: úplný backup a disaster-recovery kontrakt

Tento runbook je autoritativní pro Postiz v `/srv/postiz`. Odděluje běžné
homelab dumpy od úplného Postiz recovery setu a popisuje i stav, který zatím
není bezpečné aktivovat. Žádný krok označený jako rollout se nesmí provést bez
nezávislého auditu konkrétního commitu a samostatného schválení živé změny.

## Co znamená úspěšný recovery set

Úplný set vzniká pouze z jednoho bounded writer fence. Jeho autoritou je
WAL-konzistentní fyzická kopie celého PostgreSQL 17 clusteru. Přenosné logické
dumpy jsou druhá, striktně testovaná cesta; nejsou skládány z různých časů.

| Stav | Přesný rozsah | Důkaz obnovy |
|---|---|---|
| PostgreSQL globals | role, atributy a membership; heslové hashe se nelogují | `psql -v ON_ERROR_STOP=1`, canonical role fingerprint |
| PostgreSQL cluster | non-template katalog přesně `insights`, `postgres`, `postiz`, `temporal`, `temporal_visibility` | `pg_verifybackup`, start PG17 s `--network none`, katalogové a datové invariants |
| Logické DB fallbacky | přesně `postiz`, `temporal`, `temporal_visibility`, `insights` | každá přes `pg_restore --exit-on-error`, včetně ownerů a ACL |
| Maintenance DB | `postgres` se nedumpuje logicky jen pokud má 0 user objektů | exact inventory a zero-object receipt pod fence |
| Temporal state | Temporal DB, visibility DB a jejich `schema_version` | tabulky, workflow/execution counts a row fingerprints |
| Redis | stabilní `SAVE` po zastavení app/Temporal, exact RDB + root/file UID/GID/mode | `redis-check-rdb`, isolated load, `loaded + TTL-expired == RDB keys` |
| Runtime config | šifrovaný exact allowlist včetně `postiz.env`, Compose, image source, scheduleru, recovery toolingu, unitů, tmpfiles a source commitu | member/owner/mode/hash kontrola, source-full i resolved Compose hashes, exact Postiz `--no-deps` effective hash a image-inspect identity/default-env proti exact containerům |
| `postiz-config` volume | exact root-owned archive | network-none extract a metadata/hash kontrola |
| Uploads | `postiz_postiz-uploads`, maximálně 100 000 souborů / 16 GiB | každý obnovený soubor má exact path, size, mode a SHA-256 |
| Seasonal rollback state | policy + `seasonal-releases` + `seasonal-anchor-replacement`, nebo doložený pre-apply `absent` | schema/role/inventory SHA, root mode `0700`, bez symlinků/hardlinků |
| Images | exact běžící image ID služeb `postiz`, `postiz-postgres`, `postiz-redis`, `postiz-temporal` | čtyři Docker archives: config ID, každý layer `diff_id`, inode/byte bound a offline `docker image load` |

Postiz nemá tabulku `public."_prisma_migrations"`; aktuální kontrakt proto
receiptuje její očekávanou absenci. Temporal a Temporal Visibility mají každá
jednu `public.schema_version`. Změna kteréhokoli z těchto faktů zastaví backup a
vyžaduje review, nikoli automatické oslabení testu.

## Konzistence a dostupnost

Nightly capture používá `/run/homelab-backup/postiz-mutation.lock` a durable
root-only journal vytvořený a fsyncnutý před prvním stopem. Před fence ověří:

- exact container ID/image ID, source-full i resolved Compose model/hash všech čtyř
  služeb a samostatný Postiz effective label hash modelu bez pouze
  `postiz.depends_on`;
- bijektivní service→container→image-inspect identity a exact runtime environment
  vzniklé přepsáním image defaults explicitními Compose hodnotami, bez extra,
  chybějících nebo duplicitních klíčů;
- exact persistent mounts, cross-checked container/network-inspect endpoint IDs,
  aliases, IPv4/prefix, bridge Gateway (u swarm overlay exact prázdný container
  Gateway), disabled IPv6/IPv6Gateway, MAC a nulové host port
  bindings. Privátní síť je exact `bridge/local` s 64hex ID a pouze aktuálními
  čtyřmi Postiz endpointy; sdílená `dokploy-network` je `overlay/swarm` s
  25znakovým lower-alnum ID a binduje pouze očekávaný Postiz endpoint, zatímco
  ostatní 64hex členy se validují strukturálně bez zmrazení jejich inventory.
  Role jsou pevná matice, ne tvrzení dodané Compose modelem: `postiz` musí být
  právě na private+shared, PostgreSQL/Redis/Temporal právě na private. Rezervovaná
  jména těchto čtyř služeb smí v network inspect patřit pouze exact očekávanému
  container ID v dané síti a capture fázi; po writer fence se respektuje dočasné
  jméno PostgreSQL a žádný cizí člen nesmí spoofovat původní service name.
  Každé endpoint `Name` musí být už v canonical lowercase ASCII tvaru bez
  trailing dot; unikátnost i reserved-name binding se vyhodnocují nad canonical
  DNS jménem v každé síti.
  Jediná povolená ne-64hex položka overlaye je povinný
  `lb-dokploy-network` s exact názvem `dokploy-network-endpoint` a validním,
  nekolidujícím endpoint ID/IP/MAC. Name/ID, driver, scope, internal/attachable/
  ingress a IPv4/IPv6 flags jsou fail-closed. Bridge options jsou exact `{}`;
  overlay má jedinou option `com.docker.network.driver.overlay.vxlanid_list`
  s canonical decimálním VNI v rozsahu VXLAN, pro tento live baseline exact
  `4097`. IPAM je exact default driver/null options. Network source→resolved
  names i NetworkID jsou bijektivní;
  endpoint ID jsou globálně unikátní a IPv4/MAC unikátní v každé síti. IPv4
  IPAM subnets jsou canonical, vzájemně se nepřekrývají a mají usable in-subnet
  Gateway; endpoint nesmí být network, broadcast ani Gateway adresa. Preflight
  vyžaduje čtyři stabilně
  `running` kontejnery a active endpointy; jejich `FinishedAt` je Docker zero
  sentinel nebo syntakticky validní nenulový Docker timestamp. Writer-fenced stav vyžaduje
  stabilně `running` pouze přejmenovaný Postgres; tři zastavené writery musí být exact
  `exited`, bez restart/dead/paused stavu, s exit code 0, validním `FinishedAt` a
  prázdnými endpoint fields;
- interní network membership pouze těchto čtyř exact ID;
- přesný DB inventory, nulové user objekty v `postgres`, žádné dlouhé transakce,
  lock waitery ani prepared transactions;
- volné bajty/inody a source byte/inode ceilings ještě před velkým zápisem.

Stop pořadí je Postiz → Temporal → Redis. PostgreSQL zůstane běžet, ale jeho
canonical Docker jméno se dočasně odebere, takže známé host skripty nemohou
udělat `docker exec postiz-postgres`. Po drainu všech client backendů vzniknou
globals, čtyři logické dumpy, physical basebackup s časným limitem 1 000 000
členů/inodů, Redis/config/upload/operator
snapshoty a všechny fingerprinty. Restart je PostgreSQL → Redis → Temporal →
Postiz, s exact ID/image a bounded readiness. Temporal health vždy používá
`--address postiz-temporal:7233 --namespace default --env-file /dev/null` a
root-safe `HOME`.

Writer downtime má hard ceiling 300 s. Vnější supervisor ukončí i Bash čekající
na foreground command; EXIT/ERR/INT/TERM/HUP trap a boot recovery unit obnovují
exact původně běžící kontejnery. SIGKILL/reboot řeší durable journal a opakovaný
`postiz-quiesce-recover.service`. Commit marker nesmí vzniknout, dokud exact
writer restart a readiness neprojdou.

Všechny operator-driven Compose mutace musí jít přes
`/usr/local/sbin/postiz-compose-locked.sh`. Přímé `docker compose`, Dokploy
recreate nebo odstranění containeru podle ID během rename okna obchází lease a
je nepodporovaný root-admin zásah. Rollout proto musí zároveň prokázat, že pro
tento Compose project není aktivní jiný auto-deployer. Root uživatel a Docker
daemon zůstávají součástí trusted computing base.

Frequent timer je jiný produkt: každých 10 minut vytváří primary-only,
samostatně konzistentní per-DB PIT dumpy. Pro Postiz zahrnuje přesně čtyři
aplikační/Temporal DB, ale není cross-DB snapshot, neobsahuje globals,
Redis/config/uploads/images a není úplný service RPO. Overall `last-ok` se
atomicky posune jen po všech požadovaných dumpech a remote checksumu; alert
window je 45 minut.

## R2 layout a server-side retention

Celý timestamped payload i marker jsou v jednom zamykatelném prefixu:

```text
postiz/
├── recovery-sets/YYYY-MM/<UTC>/
│   ├── <encrypted payloads>
│   ├── recovery-set.json.enc
│   └── COMMITTED.hmac.json          # vždy poslední
├── uploads/
│   ├── manifests/YYYY-MM/uploads-<UTC>.json.enc
│   └── blobs/sha256/ab/<plaintext-sha256>.enc
└── images/sha256/<docker-image-id>.docker.tar.gz.enc
```

Cloudflare pravidla musí být sémanticky přesně tato; ID mohou být libovolná,
ale stabilní a unikátní:

| Prefix | Primary lock | DR lock | Primary lifecycle | DR lifecycle |
|---|---:|---:|---:|---:|
| `postiz/recovery-sets/` | Age 2 592 000 s | Age 7 776 000 s | delete 2 678 400 s | delete 7 862 400 s |
| `postiz/uploads/manifests/` | Age 2 592 000 s | Age 7 776 000 s | delete 2 678 400 s | delete 7 862 400 s |
| `postiz/uploads/blobs/sha256/` | Indefinite | Indefinite | žádné | žádné |
| `postiz/images/sha256/` | Indefinite | Indefinite | žádné | žádné |

Navíc zůstane právě jedno default multipart-abort pravidlo: empty prefix,
604 800 s. Žádné broad/duplicitní/extra lock nebo lifecycle pravidlo neprojde.
CAS prefix nesmí mít expiry: starý blob/image může poprvé odkazovat nový set.

`rclone --immutable` je pouze klientský collision guard. Cloudflare Bucket Lock
vynucuje delete/overwrite retention proti bucket-scoped S3 credentialu, ale
bucket-config admin/Super Admin může samotné pravidlo změnit nebo odstranit.
Nejde tedy o absolutní WORM proti control-plane kompromitaci. Primary a DR jsou
navíc ve stejném R2 účtu/provideru; tato korelovaná admin failure domain je
vědomý residual risk.

Runtime remotes `r2postiz` a `r2drpostiz` mají oddělené bucket-scoped Object
Read & Write klíče. Tato permission zahrnuje `DeleteObject`; skripty delete na
Postiz prefixes nevolají, ale enforcement dává Bucket Lock. Každý remote musí
mít vlastní bucket, přesný default-account endpoint a explicitní cross-bucket
`403/AccessDenied`. Ambient `RCLONE_CONFIG_*` je odříznut přes `env -i`.

Lock/lifecycle čte samostatný account-scoped Cloudflare R2 Storage Read token.
Token je root:root `0600`, nejde do argv/env/logu a curl běží bez curlrc, proxy a
keylog ambientu. Oba živé buckety jsou v default jurisdiction; bucket token
resource musí mít tvar
`com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_default_<BUCKET>`. Varianta `_eu_`
je pro tento stav chybná, i když bucket používá evropský location hint.

Nightly attester načte `GET /accounts/<id>/r2/buckets/<bucket>/lock` a
`.../lifecycle`, ověří přesné `maxAgeSeconds`/`maxAge` semantics a přidá
krátkodobý policy receipt do authenticated recovery setu. Historický receipt
musí projít při každém drillu. Aktuální policy se znovu atestuje. Jen explicitní
transport/API unavailability (`EX_TEMPFAIL=75`, například timeout/429/5xx) dovolí
drillu pokračovat výhradně s authenticated historical receipt. Semantic
lock/lifecycle drift, credential scope, cross-bucket denial nebo local-config
chyba vždy drill zastaví. Nightly backup aktuální attestation vždy vyžaduje a bez
ní nový marker nevytvoří.

## Šifrování a autenticita

Payloady používají salted OpenSSL AES-256-CBC/PBKDF2 kvůli kompatibilitě se
stávajícím off-box klíčem. Samotné CBC není autentizované. Recovery marker je
proto chráněn odděleně odvozeným HMAC klíčem a HMAC pokrývá context, filename,
size i všechny ciphertext bytes. Restore ověří HMAC a timestamp context před
decryptem; marker pak váže SHA-256 a size každého ciphertext payloadu.

Upload CAS se při každém úspěšném běhu stáhne z obou remotes v bounded current
setu a každý ciphertext se dešifruje proti plaintext content address. Existing
image archive se také stáhne, dešifruje a ověří z každé přítomné kopie. Tím
neprojde preplay objektu se správným názvem/velikostí. Testy pokrývají bit flip,
truncation, wrong key a context replay. Žádná secret hodnota se nevypisuje.

## Kapacita a očekávaný náklad

Live read-only baseline z 2026-08-14:

- uploads: 10 382 files, 4 043 343 661 B logical; 8 246 unique SHA-256 a
  2 663 083 992 B unique plaintext;
- čtyři běžící images: raw `.Size` dohromady 4 465 404 688 B
  (Postiz 3 626 317 238; PG 297 072 501; Redis 112 420 059; Temporal
  429 594 890 B);
- existující oba R2 buckety dohromady 35 818 778 216 B před rolloutem.

První CAS upload proto očekává přibližně 2.664 GB ciphertext do každého bucketu
plus padding. Přesná velikost čtyř compressed Docker archives se musí získat v
schváleném prvním běhu; hard gate je `<5 GiB` na image a `<=12 GiB` nových image
ciphertextů za běh. Další nightly běhy nereuploadují nezměněné uploady ani
images: přidají jen nové unique CAS objekty, malé manifests a nový timestamped
recovery set. Current-set CAS se kvůli anti-preplay validaci z obou bucketů
stahuje; maximální download envelope je 2 × 16 GiB, nikoli nový storage zápis.

Další hard bounds: 8 GiB nového upload ciphertextu/run, 96 GiB capture
workspace, 192 GiB restore peak, 1 000 000 expanded image-layer inodes a combined
StateDirectory/DockerRootDir kontrola, pokud leží na stejném filesystemu.
Crossing ceiling selže před velkým fetchem/zápisem, nebo přes RLIMIT u
streamovaného dump/archive outputu. Storage cenu je nutné po prvním setu
porovnat s receipt bytes; žádný odhad není důvod limit automaticky zvednout.

## Weekly strict restore drill

`restore-drill.timer` vybere nejnovější společný canonical set, jehož marker i
HMAC projdou z obou bucketů. Invalidní/replayed append-only candidate přeskočí a
zalarmuje; nemůže zakrýt starší validní set. Listing má byte/entry ceiling.

Každý remote se obnoví samostatně. Drill:

1. ověří aktuální i historický R2 policy receipt a authenticated marker;
2. preflightuje všechny deklarované ciphertext/plain/expanded bajty a inody;
3. obnoví exact runtime config, config volume, uploads a seasonal state;
4. ověří a offline načte všechny čtyři Docker archives bez pullu;
5. aplikuje globals a čtyři logické dumpy do PG17 přes
   `pg_restore --exit-on-error`, včetně owner/ACL fingerprints;
6. spustí `pg_verifybackup`, pak fyzický cluster a porovná stejné DB/count/
   catalog/role/Temporal invariants;
7. strukturálně ověří Redis RDB, zachová UID/GID/modes a testuje TTL-aware load;
8. vykreslí fresh-host Compose image override a dokáže přesné service → image ID.

Všechny parser/DB/Redis kontejnery mají `--network none`, žádné host porty a
žádné production volumes. Mají také hard RAM/swap/PID/CPU limity. Logické dumpy
jsou read-only bind mounts, ne kopie do malého tmpfs. Runtime config má 16 MiB
per-member a 64 MiB aggregate expanded limit započtený do restore peak; config
volume a seasonal archivy mají vedle byte limitu pevný member/inode strop a sealing streamuje soubory,
takže je nedrží celé v RAM. Drill nikdy nepoužije `docker compose
down/stop/restart` na live project a nezapisuje do live volumes.

Předcházející generic drill stage má samostatný bounded remote listing, 2 GiB
per-object ciphertext/plaintext ceiling, StateDirectory+Docker byte/inode preflight
a available-memory gate. Dump je pouze read-only bind; PGDATA je 8 GiB tmpfs pod
10 GiB cgroup memory limitem a každý ověřený DB se hned dropne. Tím generic stage
nemůže přes compressed dump neomezeně zaplnit host storage před Postiz drillem.
Jeho historické Freio/Ripieno/Lokwave CBC objekty a tolerantní data heuristic
nejsou součástí authenticated strict Postiz recovery contractu; jejich migrace
na EtM a strict restore vyžaduje samostatný kompatibilitní rollout produceru.

Před prvním plaintextem vzniknou fsyncnuté exact journals pro generic a Postiz
stages (`generic-restore-active.json`, `postiz-restore-active.json`) s workspace a
deterministickými container names/labels. Generic stage se uklidí ještě před
dlouhým Postiz drillem. EXIT cleanup, `ExecStopPost` a boot
`postiz-restore-cleanup.service` odstraňují pouze objekty svázané s journals;
label/path drift failne bez mazání. Tím se po TERM, SIGKILL, timeoutu nebo rebootu
nenechá root-only plaintext ani detached parser/DB/Redis container.

Nightly, frequent, artifact a raw policy-response workspaces mají samostatné
root-only producer locks. Každý producer po získání exact lock FD reapne pouze svůj
šestiznakový prefix. `postiz-backup-workspace-cleanup.service` dělá stejný bounded
reap při bootu a `ExecStartPre`/`ExecStopPost`; active locked scope vždy ponechá.

## Fresh-host nebo in-place disaster restore

Nejdřív spusť strict drill na izolovaném recovery hostu se stejným reviewed
tooling commitem. Teprve jeho zelený primary i DR výsledek dovoluje cutover.

1. Z off-box zdrojů vrať backup passphrase, dva bucket-scoped object remotes,
   read-only policy token a policy source. Ověř root ownership/modes a endpoint/
   account binding; secret hodnoty neukládej do shell history.
2. Checkoutni exact 40hex commit z
   `/etc/homelab/postiz-backup-source-revision`; verify tooling hashes před
   spuštěním. Registry ani Git nejsou autoritou pro čtyři runtime images.
3. Vyber jeden HMAC-validní committed timestamp. Nemíchej payloady z jiných
   timestampů ani bucketů. Nejdřív obnov a ověř všechny čtyři Docker archives.
4. Vytvoř nové recovery volumes/project. Nikdy nerozbaluj přímo do existujícího
   `postiz_postiz-postgres`, `postiz_postiz-redis`, `postiz_postiz-config` nebo
   `postiz_postiz-uploads` volume.
5. Rozbal physical PG cluster do nového PG volume s archivovanými metadaty a
   ověř `pg_verifybackup`. Logická cesta je fallback: aplikuj globals, vytvoř
   přesně čtyři DB s ownerem `postiz`, obnov každou bez `--no-privileges` a
   zopakuj všechny fingerprints. Maintenance `postgres` zůstane bez user objektů.
6. Vrať config volume, uploads, Redis RDB a případný seasonal state do nových
   root-only roots s exact UID/GID/modes; policy musí po apply vyžadovat oba
   seasonal roots a jejich exact inventory.
7. Vygeneruj override s image `sha256:<loaded-id>` a `pull_policy: never` pro
   každou službu. Validation project musí mít pouze interní/no-egress network,
   unikátní container names, žádné porty a nové volume names. Spusť health,
   Temporal workflow/catalog, Postiz data/upload a Redis checks bez public route.
8. U in-place obnovy zastav starý project až po zelené validaci. Proveď atomický
   route/volume-name cutover na nové volumes a ponech staré volume read-only pro
   rollback. Na fresh hostu připoj external route až v posledním kroku.
9. Po cutoveru udělej nový writer-fenced backup; ten musí zaznamenat nové exact
   container IDs/images a případný seasonal `present` stav.

Physical cluster je autoritativní full-recovery cesta. Logical dumps jsou
portable fallback, ale jsou také plně testované včetně globals, owners a ACL.

## Audited rollout

### Povinné gates před instalací

- reviewed source ancestor je `8ec0e5535879d93bb0ce0df556b291d1228f2315`;
  `/etc/homelab/postiz-backup-source-revision` však musí obsahovat výsledný
  reviewed commit, ne ancestor ani dirty tree;
- R2 dnes nesmí být považováno za chráněné, dokud attester neprokáže všechna
  lock/lifecycle pravidla a cross-bucket denial;
- Docker Compose `5.3.1` dává Postiz source modelu s deklarací `env_file`
  `3f88a262802d14fa0401a71abf751153132250408a3a6d55fdee2cb2842999db`.
  Full resolved JSON po opětovném zpracování přes Compose má hash
  `6e0fb11e0ae8b8ab9f6188ee0ead6a66b16835e530284c5413842cac2af40eb1`,
  protože už nenese source `env_file` provenance. Odstranění pouze
  `services.postiz.depends_on` tento resolved hash nemění; Compose `5.3.1`
  `depends_on` do service hashe nezahrnuje. Auditovaný controlled recreate přes
  `up --no-deps` vytvořil právě effective label `6e0…`. Nejde o
  environment/image/mount/network drift. Capture musí fail-closed přepočítat
  source-full, full-resolved a exact no-deps model, dokázat jejich přesné vztahy,
  porovnat Postiz label s effective hashem a zbývající tři labels se source-full
  hashes. Jakýkoli jiný rozdíl zastaví běh před prvním stopem;
- `/run/homelab-backup` a `/var/lib/freio-content` se vytvoří tmpfiles pravidlem
  před schedulery; nevytvářej je ad hoc s jinými právy;
- všechny Postiz deploy/recreate cesty musí používat shared mutation lock.

### Instalace exact reviewed bytes

Nejdřív ulož root-only kopii každého nahrazovaného souboru, jeho SHA-256 a
enabled/active stav timerů. Proměnná `REVIEWED_COMMIT` musí být exact 40hex SHA
nezávisle auditovaného commitu.

```bash
sudo install -o root -g root -m 0755 scripts/postiz-backup-manifest.py /usr/local/libexec/postiz-backup-manifest.py
sudo install -o root -g root -m 0755 scripts/postiz-artifact-backup.sh /usr/local/sbin/postiz-artifact-backup.sh
sudo install -o root -g root -m 0755 scripts/postiz-backup-workspace-cleanup.sh /usr/local/sbin/postiz-backup-workspace-cleanup.sh
sudo install -o root -g root -m 0755 scripts/postiz-compose-locked.sh /usr/local/sbin/postiz-compose-locked.sh
sudo install -o root -g root -m 0755 scripts/postiz-quiesced-capture.sh /usr/local/sbin/postiz-quiesced-capture.sh
sudo install -o root -g root -m 0755 scripts/postiz-r2-policy-attest.sh /usr/local/sbin/postiz-r2-policy-attest.sh
sudo install -o root -g root -m 0755 scripts/backup.sh /usr/local/bin/homelab-backup.sh
sudo install -o root -g root -m 0755 scripts/frequent-db-backup.sh /usr/local/bin/frequent-db-backup.sh
sudo install -o root -g root -m 0750 self-healing/postiz-offline-verify.sh /srv/homelab/self-healing/postiz-offline-verify.sh
sudo install -o root -g root -m 0750 self-healing/postiz-restore-drill.sh /srv/homelab/self-healing/postiz-restore-drill.sh
sudo install -o root -g root -m 0750 self-healing/restore-drill.sh /srv/homelab/self-healing/restore-drill.sh
sudo install -o root -g root -m 0644 scripts/tmpfiles.d/homelab-backup.conf /etc/tmpfiles.d/homelab-backup.conf
sudo install -o root -g root -m 0644 scripts/systemd/backup.service /etc/systemd/system/backup.service
sudo install -o root -g root -m 0644 scripts/systemd/frequent-db-backup.service /etc/systemd/system/frequent-db-backup.service
sudo install -o root -g root -m 0644 scripts/systemd/restore-drill.service /etc/systemd/system/restore-drill.service
sudo install -o root -g root -m 0644 scripts/systemd/postiz-quiesce-recover.service /etc/systemd/system/postiz-quiesce-recover.service
sudo install -o root -g root -m 0644 scripts/systemd/postiz-backup-workspace-cleanup.service /etc/systemd/system/postiz-backup-workspace-cleanup.service
sudo install -o root -g root -m 0644 scripts/systemd/postiz-restore-cleanup.service /etc/systemd/system/postiz-restore-cleanup.service
printf '%s\n' "$REVIEWED_COMMIT" | sudo install -o root -g root -m 0644 /dev/stdin /etc/homelab/postiz-backup-source-revision
sudo systemd-tmpfiles --create /etc/tmpfiles.d/homelab-backup.conf
```

Reassert exact existing timer files (`backup.timer`, `frequent-db-backup.timer`,
`restore-drill.timer`), `/srv/postiz/{postiz.env,docker-compose.yml,
Dockerfile.patch,schedule-week.py}` modes and root ownership. Instaluj root:root
`0600` policy source, read token a rclone config podle example bez vypsání
hodnot. Pak:

```bash
sudo systemd-analyze verify /etc/systemd/system/backup.service /etc/systemd/system/frequent-db-backup.service /etc/systemd/system/restore-drill.service /etc/systemd/system/postiz-quiesce-recover.service /etc/systemd/system/postiz-backup-workspace-cleanup.service /etc/systemd/system/postiz-restore-cleanup.service
sudo systemctl daemon-reload
sudo systemctl enable --now postiz-quiesce-recover.service
sudo systemctl enable --now postiz-backup-workspace-cleanup.service
sudo systemctl enable --now postiz-restore-cleanup.service
sudo /usr/local/sbin/postiz-r2-policy-attest.sh
```

Nezapínej timery. Nejdřív musí být zelený source/resolved/effective hash gate:
source-full model, full resolved model, exact Postiz `depends_on`-only projection,
Postiz effective label a source-full labels zbývajících tří služeb. Controlled
recreate smí jít jen přes
`postiz-compose-locked.sh`; nesmí se snažit vynutit full Postiz label a současně
používat `--no-deps`. Potom ověř všechny container IDs/images/health/topology a
spusť jeden supervised `backup.service`. Teprve poté spusť `restore-drill.service`.

### Go acceptance

- tmpfiles jsou po dvou `systemd-tmpfiles --create` stále root-owned exact
  `0700/0600`, bez symlinků;
- attester potvrzuje default-jurisdiction account/bucket binding, exact locks,
  lifecycle a explicitní cross-bucket denial;
- preflight source-full i resolved Compose hashes jsou exact; Postiz resolved a
  `--no-deps` hashes jsou shodné a running label jim odpovídá, other3 source-full
  a resolved hashes jsou shodné a odpovídají jejich labels. Full model je navázán
  na unique container IDs, exact Docker image-inspect IDs, merged image-default +
  Compose environment, stable phase-specific container state a bijektivní,
  cross-checked network/IPAM/alias/IP/IPv6/MAC/mount/port topology;
- fault injection TERM/HUP/timeout obnoví pouze původně running exact IDs v
  pořadí PG→Redis→Temporal→Postiz; stale journal přežije reboot a retry;
- jeden marker vznikne až poslední v obou `recovery-sets` prefixes a jeho HMAC,
  payload hashes a timestamp context projdou;
- první storage receipt odpovídá schválenému byte/cost envelope;
- strict drill projde samostatně z primary i DR pro globals + fyzický cluster +
  čtyři logické DB + Redis + config + config volume + uploads + seasonal state +
  čtyři images, vše bez network/port/prod-volume přístupu;
- production IDs, data counts a volume content se drillem nezmění a nezůstanou
  žádné throwaway kontejnery/plaintext workspace; explicitní SIGKILL/reboot
  fixture prokáže boot reaper, odstranění obou durable restore journals a exact
  cleanup `nightly/frequent/postiz-artifact/postiz-policy` workspaces;
- reálný `docker image save <ID>` z rollout Docker Engine projde hybridní
  Docker-archive graph/diff-ID verifierem a následným offline `docker image load`.

Dokud nejsou všechny body doloženy, stav je NO-GO a timery zůstávají v původním
stavu.

## Rollback

Nejdřív spusť `postiz-quiesced-capture.sh --recover-only`; pokud zůstává journal
nebo readiness neprojde, rollback se zastaví a eskaluje. Potom zastav pouze
nově zapnuté backup/frequent/restore timery, obnov exact uložené skripty, unity a
tmpfiles podle jejich SHA, proveď `daemon-reload` a vrať původní enabled/active
stav. Candidate-only soubory lze po úspěšném writer recovery odstranit, ale
`/var/lib/homelab-backup/postiz-quiesce-journal.json` se nikdy nemaže ručně.
Stejně tak se ručně nemaže žádný restore journal; nejdřív spusť
`/srv/homelab/self-healing/restore-drill.sh --cleanup-only`. Stale backup
workspaces reapni pouze přes `postiz-backup-workspace-cleanup.sh --scope all`;
při identity/label/path/mount driftu eskaluj.

Objekty pod `postiz/` nemaž: jsou encrypted, server-locked a mohou být jediná
platná recovery kopie; expiry řídí lifecycle. Pokud rollout zahrnul controlled
container recreate, vrať starý exact image/config pouze přes shared mutation
wrapper a ověř health. Rollback nikdy nedělá `pg_restore` do production volume,
nepřepisuje uploads/Redis/PG volume a nemění R2 lock rules naslepo.
