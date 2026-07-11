# Migrace souborů: Supabase Storage → Cloudflare R2 (nebo self-hosted Storage)

Které projekty mají soubory v Supabase Storage:

| Projekt | Bucket(y) | Co je uvnitř | URL v DB? |
|---------|-----------|--------------|-----------|
| **freio** | `marketing-assets` (public) | loga partnerských škol | ✅ `approved_institutions.logo_path` (někdy Supabase URL, někdy lokální `/logos/...`) |
| **dentallocal** | `reports` (private) | PDF reporty (signed URL) | odvozené, ne uložené |
| **explain-and-act** | `documents` (private, per-user složky) | naskenované dokumenty uživatelů | cesty v `documents` tabulce |
| **life-admin-agent** | `documents` | dokumenty k nárokům (přílohy mailů) | cesty v `documents` |
| **agent-farm** | `media` | vygenerovaná média | cesty v `media_assets` |
| **ripieno** | `discovery-uploads`, raw-log bucket | přílohy + archiv event logů | signed/odvozené, ne uložené |
| **leadcrm** | `leads` | CSV, screenshoty, HTML těl mailů | přes `publicUrl(path)` builder |
| claude-trader, hummy, nate_trader, openClawTrader, teriProjekt | — | žádné soubory | — |

Dvě cesty (podle zvolené strategie u daného projektu):

- **Strategie A (self-hosted Supabase):** nech `supabase.storage.*` volání a jen
  přesměruj Storage backend self-hosted `storage-api` na **filesystem volume** nebo
  **S3/R2**. Kód se nemění. Nejjednodušší, když už jedeš Strategii A.
- **Strategie B (holý stack):** nahraď `supabase.storage.*` za **S3 klienta na R2**.
  Tenhle dokument popisuje hlavně B (a přenos samotných souborů, který je nutný tak jako tak).

---

## 1) Zkopíruj samotné soubory (nutné v obou strategiích)

Vytáhni objekty ze Supabase Storage a nahraj do R2. Nejjednodušeji přes **rclone**
(oba jsou S3-kompatibilní):

```ini
# ~/.config/rclone/rclone.conf
[supabase]
type = s3
provider = Other
access_key_id = <SUPABASE_STORAGE_S3_ACCESS_KEY>      # Supabase → Settings → Storage → S3 access keys
secret_access_key = <...>
endpoint = https://<ref>.supabase.co/storage/v1/s3
region = <region>

[r2]
type = s3
provider = Cloudflare
access_key_id = <R2_KEY>
secret_access_key = <R2_SECRET>
endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```
```bash
# založ R2 bucket per projekt/bucket, pak zkopíruj (idempotentní, dá se opakovat):
rclone copy "supabase:marketing-assets" "r2:freio-marketing-assets" -P
rclone check "supabase:marketing-assets" "r2:freio-marketing-assets"   # ověření, že sedí
```
> Když Supabase S3 přístup nemáš, jde stáhnout přes REST list+download API bucketu
> (`/storage/v1/object/list/<bucket>` + `/object/<bucket>/<path>`) skriptem.

## 2) Nahraď volání v kódu (Strategie B)

Supabase Storage → S3 klient (`@aws-sdk/client-s3`) proti R2:
```ts
import { S3Client, PutObjectCommand, GetObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

const r2 = new S3Client({
  region: "auto",
  endpoint: `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  credentials: { accessKeyId: process.env.R2_ACCESS_KEY_ID!, secretAccessKey: process.env.R2_SECRET_ACCESS_KEY! },
});
```
Mapování:
| Supabase | R2 (S3 SDK) |
|----------|-------------|
| `.storage.from(b).upload(path, data)` | `PutObjectCommand({ Bucket, Key: path, Body })` |
| `.storage.from(b).createSignedUrl(path, ttl)` | `getSignedUrl(r2, new GetObjectCommand({Bucket,Key}), {expiresIn: ttl})` |
| `.storage.from(b).getPublicUrl(path)` | veřejné R2: `https://<public-r2-domain>/<path>` (nastav R2 public bucket / custom domain) |
| `.storage.from(b).remove([path])` | `DeleteObjectCommand({Bucket, Key})` |

## 3) Přepiš URL uložené v databázi (kde jsou)

Sloupce s Supabase Storage URL musíš přepsat na R2:
```sql
-- freio: approved_institutions.logo_path (jen ty s Supabase URL, lokální /logos/ nech)
UPDATE approved_institutions
SET logo_path = replace(logo_path,
  'https://<ref>.supabase.co/storage/v1/object/public/marketing-assets/',
  'https://<r2-public-domena>/')
WHERE logo_path LIKE '%supabase.co/storage%';
```
Podobně: leadcrm `publicUrl` builder (přepiš string builder na R2 doménu),
dentallocal/explain-and-act/life-admin-agent/agent-farm/ripieno — jejich cesty
jsou většinou relativní (jen `path`), takže se mění jen base URL v klientovi, ne data.

## 4) Uprav config / CSP

Kde je Supabase Storage povolený v `next.config`/CSP, přepiš na R2:
```js
// freio next.config.mjs — images.remotePatterns: '**.supabase.co' → '<r2-public-domena>'
// CSP img-src / connect-src: odeber *.supabase.co, přidej R2 doménu
```

## 5) Ověření

- [ ] `rclone check` prošel (počet + hash objektů sedí)
- [ ] Náhodné soubory se načtou z nové URL (obrázek/PDF v appce)
- [ ] Signed URL fungují (private buckety: reports, documents)
- [ ] Nové uploady jdou do R2 a čtou se zpět
- [ ] Supabase bucket nech nedotčený jako fallback, než ověříš pár dní

## Poznámka: R2 zdarma stačí

R2 free = 10 GB. Loga, PDF reporty a dokumenty jsou většinou malé. Kdyby některý
projekt měl hodně velkých médií (agent-farm generovaná média), sleduj objem —
R2 nad 10 GB je $0,015/GB/měs (haléře). Zálohy DB máme ve stejném R2 účtu.
