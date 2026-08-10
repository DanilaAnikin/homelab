#!/usr/bin/env python3
# Recurring: přiřadí nativní licencovanou hudbu KAŽDÉMU budoucímu IG video Reelu, který ještě
# hudbu nemá (nové posty z content-engine). Respektuje 30denní no-repeat vůči už přiřazeným.
# Idempotentní: existující přiřazení nemění. Cron á 6h. Bank obnovuje, když chybí/je prázdná.
import json, subprocess, sys, random, datetime, os

def psql(sql, tuples=True):
    r = subprocess.run(["docker","exec","postiz-postgres","psql","-U","postiz","-d","postiz","-tAF","\t","-c",sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:600]); sys.exit(1)
    out = [ln for ln in r.stdout.strip().split("\n") if ln]
    return [ln.split("\t") for ln in out] if tuples else out

BANK = "/srv/homelab/scripts/postiz-audio-bank.json"
if not os.path.exists(BANK):
    print("Bank chybí, spusť audio_bank.py"); sys.exit(1)
bank = json.load(open(BANK))
random.seed()  # náhodné pořadí pro nové běhy

WINDOW = datetime.timedelta(days=30)

# Existující přiřazení (per účet) — abychom neopakovali track v 30denním okně
existing = psql("""
  select i."internalId", p."publishDate", p.settings::jsonb->'audio'->>'id'
  from "Post" p join "Integration" i on i.id=p."integrationId"
  where i."providerIdentifier"='instagram' and p."deletedAt" is null and p.state='QUEUE'
    and p."publishDate">now() and p.settings::jsonb->'audio' is not null
  order by i."internalId", p."publishDate";""")
recent_by_acct = {}
for iid, pdate, aid in existing:
    dt = datetime.datetime.fromisoformat(pdate.replace(" ","T").split("+")[0])
    recent_by_acct.setdefault(iid, []).append((aid, dt))

# Reels bez hudby
missing = psql("""
  select p.id, i."internalId", p."publishDate"
  from "Post" p join "Integration" i on i.id=p."integrationId"
  where i."providerIdentifier"='instagram' and p."deletedAt" is null and p.state='QUEUE'
    and p."publishDate">now()
    and (p.settings::jsonb->>'post_type')='post'
    and (p.image::text ilike '%.mp4%' or p.image::text ilike '%video%')
    and p.settings::jsonb->'audio' is null
  order by i."internalId", p."publishDate";""")

if not missing:
    print("Nic nového bez hudby."); sys.exit(0)

count = 0
for pid, iid, pdate in missing:
    tracks = list(bank.get(iid, {}).get("tracks", []))
    if not tracks: continue
    random.shuffle(tracks)
    dt = datetime.datetime.fromisoformat(pdate.replace(" ","T").split("+")[0])
    recent = [(a,d) for a,d in recent_by_acct.get(iid, []) if abs((dt-d).days) < 30]
    chosen = None
    for t in tracks:
        if not any(a == t["id"] for a,_ in recent):
            chosen = t; break
    if not chosen: chosen = tracks[0]
    recent_by_acct.setdefault(iid, []).append((chosen["id"], dt))
    audio = {"id": chosen["id"], "title": chosen["title"], "artist": chosen["artist"],
             "audio_volume": 100, "video_volume": 55}
    aj = json.dumps(audio, ensure_ascii=False).replace("'", "''")
    psql(f"""update "Post" set settings=(settings::jsonb || '{{"audio": {aj}}}'::jsonb)::text where id='{pid}';""", tuples=False)
    count += 1

print(f"Přiřazena hudba {count} novým IG Reelům.")
