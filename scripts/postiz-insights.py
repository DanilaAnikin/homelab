#!/usr/bin/env python3
"""
insights.py — stahuje výkon publikovaných postů z Meta Graph API do vlastní DB.

PROČ: Postiz si o výkonu neukládá NIC. Bez toho se obsah nedá zlepšovat jinak než
odhadem — 45 publikovaných postů a nula informací o tom, který fungoval.

Sleduje hlavně to, co řídí dosah, ne ješitnost:
  reach   — kolika lidem se to ukázalo
  saved   — uložení (u vzdělávacího obsahu nejsilnější signál)
  shares  — sdílení
  views   — přehrání u reelů
  + navigace u stories (kolik lidí odešlo hned na prvním snímku)

Data jdou do SAMOSTATNÉ databáze `insights`, ne do `postiz`: Postiz se aktualizuje
přes `prisma db push`, který cizí tabulku ve svém schématu může zahodit.

Metriky se dotahují opakovaně (dosah roste ještě dny po publikaci), proto se ukládá
snapshot s časem a bere se vždy nejnovější.

Použití: insights.py [dny_zpetne]   (default 30)
"""
import json, subprocess, sys, urllib.parse, urllib.request, datetime

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
GRAPH = "https://graph.facebook.com/v22.0"

# Metriky podle typu média — Graph API vrátí chybu, když se zeptáš na metriku,
# kterou daný typ nepodporuje, takže se to musí rozlišit.
METRICS = {
    "REELS":    "reach,saved,shares,likes,comments,views,ig_reels_avg_watch_time",
    "FEED":     "reach,saved,shares,likes,comments,profile_visits,follows",
    "CAROUSEL": "reach,saved,shares,likes,comments,profile_visits,follows",
    "STORY":    "reach,replies,navigation",
}


def psql(db, sql, tuples=True):
    r = subprocess.run(
        ["docker", "exec", "-i", "postiz-postgres", "psql", "-U", "postiz", "-d", db,
         "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:400]); sys.exit(1)
    out = [ln for ln in r.stdout.strip().split("\n") if ln]
    return [ln.split("\t") for ln in out] if tuples else out


def api(path, token, params=None):
    q = dict(params or {}); q["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try: body = e.read().decode()[:200]
            except Exception: pass
        return {"__error": f"{e} {body}"}


# --- cílová databáze -------------------------------------------------------
exists = psql("postgres", "select 1 from pg_database where datname='insights';")
if not exists:
    psql("postgres", "create database insights;", tuples=False)
    print("databáze insights vytvořena")

psql("insights", """
create table if not exists post_insights (
  id           bigserial primary key,
  measured_at  timestamptz not null default now(),
  post_id      text not null,
  release_id   text not null,
  channel      text not null,
  provider     text not null,
  post_type    text,
  publish_date timestamptz,
  permalink    text,
  metrics      jsonb not null
);
create index if not exists ix_post_insights_release on post_insights(release_id, measured_at desc);
""", tuples=False)

# --- publikované posty -----------------------------------------------------
rows = psql("postiz", f"""
  select p.id, p."releaseId", i.name, i."providerIdentifier", i.token,
         coalesce(p.settings::jsonb->>'post_type','post'), p."publishDate"::text
  from "Post" p join "Integration" i on i.id = p."integrationId"
  where p.state='PUBLISHED' and p."deletedAt" is null and p."releaseId" is not null
    and p."publishDate" > now() - interval '{DAYS} days'
  order by p."publishDate" desc;""")

print(f"publikovaných postů k změření: {len(rows)}")
ok = fail = 0
for pid, rid, chan, prov, token, ptype, pdate in rows:
    if prov != "instagram":
        continue  # FB insights mají jiný tvar; IG je pro nás rozhodující
    meta = api(rid, token, {"fields": "media_product_type,media_type,permalink"})
    if "__error" in meta:
        print(f"  ✗ {chan} {rid}: {meta['__error'][:90]}"); fail += 1; continue
    kind = meta.get("media_product_type") or ""
    if kind == "STORY" or ptype == "story":
        key = "STORY"
    elif kind == "REELS":
        key = "REELS"
    elif meta.get("media_type") == "CAROUSEL_ALBUM":
        key = "CAROUSEL"
    else:
        key = "FEED"

    ins = api(f"{rid}/insights", token, {"metric": METRICS[key]})
    if "__error" in ins:
        # Když některá metrika u konkrétního média chybí, zkus jen ty jisté.
        ins = api(f"{rid}/insights", token, {"metric": "reach"})
        if "__error" in ins:
            print(f"  ✗ {chan} {rid} ({key}): {ins['__error'][:90]}"); fail += 1; continue

    vals = {}
    for m in ins.get("data", []):
        v = m.get("values", [{}])[0].get("value")
        vals[m.get("name")] = v
    if not vals:
        fail += 1; continue

    payload = json.dumps(vals, ensure_ascii=False).replace("'", "''")
    perma = (meta.get("permalink") or "").replace("'", "''")
    psql("insights", f"""
      insert into post_insights (post_id, release_id, channel, provider, post_type, publish_date, permalink, metrics)
      values ('{pid}', '{rid}', '{chan.replace("'","''")}', '{prov}', '{key}', '{pdate}', '{perma}', '{payload}'::jsonb);""",
         tuples=False)
    ok += 1

print(f"změřeno: {ok}, selhalo: {fail}")
