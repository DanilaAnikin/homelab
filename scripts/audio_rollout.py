#!/usr/bin/env python3
# Fáze B: přiřadí rotační licencovanou hudbu budoucím IG video Reelům (30denní no-repeat per účet).
# Fáze C: (pouze výpis do souboru) budoucí QUEUE posty k přeplánování do Temporalu.
# DRY-RUN pokud není arg "apply".
import json, subprocess, sys, random, datetime

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"
random.seed(42)  # deterministické

def psql(sql, tuples=True):
    r = subprocess.run(["sudo","docker","exec","postiz-postgres","psql","-U","postiz","-d","postiz","-tAF","\t","-c",sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:600]); sys.exit(1)
    out = [ln for ln in r.stdout.strip().split("\n") if ln]
    return [ln.split("\t") for ln in out] if tuples else out

bank = json.load(open("/srv/homelab/scripts/postiz-audio-bank.json"))

# --- Fáze B: budoucí IG video Reels (post_type=post + video), per účet, chronologicky ---
reels = psql("""
  select p.id, i."internalId", i.name, p."publishDate"
  from "Post" p join "Integration" i on i.id=p."integrationId"
  where i."providerIdentifier"='instagram' and p."deletedAt" is null and p.state='QUEUE'
    and p."publishDate" > now()
    and (p.settings::jsonb->>'post_type') = 'post'
    and (p.image::text ilike '%.mp4%' or p.image::text ilike '%video%')
  order by i."internalId", p."publishDate";""")

WINDOW = datetime.timedelta(days=30)
per_acct = {}
for pid, iid, name, pdate in reels:
    per_acct.setdefault(iid, []).append((pid, name, pdate))

assign = []  # (postId, audio_json)
for iid, items in per_acct.items():
    tracks = list(bank.get(iid, {}).get("tracks", []))
    random.shuffle(tracks)
    if not tracks:
        print(f"!! žádné tracky pro {iid}"); continue
    ti = 0
    recent = []  # (audio_id, date)
    for pid, name, pdate in items:
        dt = datetime.datetime.fromisoformat(pdate.replace(" ", "T").split("+")[0])
        # vyber track, který nebyl použit v posledních 30 dnech
        chosen = None
        for _ in range(len(tracks)):
            t = tracks[ti % len(tracks)]; ti += 1
            if not any(a == t["id"] and (dt - d) < WINDOW for a, d in recent):
                chosen = t; break
        if not chosen:
            chosen = tracks[ti % len(tracks)]; ti += 1
        recent.append((chosen["id"], dt))
        recent = [(a, d) for a, d in recent if (dt - d) < WINDOW]
        audio = {"id": chosen["id"], "title": chosen["title"], "artist": chosen["artist"],
                 "audio_volume": 100, "video_volume": 0}
        assign.append((pid, audio))

print(f"Fáze B: {len(assign)} IG Reels dostane hudbu (přes {len(per_acct)} účtů)")
for iid, items in per_acct.items():
    print(f"  {bank.get(iid,{}).get('name',iid):28s} {len(items)} reels")

if APPLY:
    for pid, audio in assign:
        aj = json.dumps(audio, ensure_ascii=False).replace("'", "''")
        psql(f"""update "Post" set settings = (settings::jsonb || '{{"audio": {aj}}}'::jsonb)::text where id='{pid}';""", tuples=False)
    print(f"  ✅ Hudba zapsána do {len(assign)} postů")

# --- Fáze C: seznam VŠECH budoucích QUEUE postů k přeplánování ---
allq = psql("""
  select p.id, i."providerIdentifier", p."organizationId"
  from "Post" p join "Integration" i on i.id=p."integrationId"
  where p."deletedAt" is null and p.state='QUEUE' and p."publishDate" > now()
  order by p."publishDate";""")
with open("/srv/homelab/scripts/postiz-reschedule.txt","w") as f:
    for pid, prov, org in allq:
        tq = prov.split("-")[0]
        f.write(f"{pid}|{tq}|{org}\n")
print(f"\nFáze C: {len(allq)} budoucích QUEUE postů zapsáno do postiz-reschedule.txt (k přeplánování)")
