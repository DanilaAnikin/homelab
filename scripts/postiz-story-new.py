#!/usr/bin/env python3
# Recurring: každou budoucí IG obrázkovou story (png/jpg) z content-enginu převede na 7s VIDEO
# s rotační royalty-free hudbou (jediný způsob, jak mít zvuk na stories). Idempotentní:
# jakmile je story mp4, už ji znovu nevezme. Běží jako ROOT cron (docker bez sudo). Cron á 6h.
import json, subprocess, sys, os, secrets, random

TRACKS = ["epic", "minimal", "calm", "upbeat", "neutral"]
VOL = "postiz_postiz-uploads"
MUSICDIR = "/srv/homelab/scripts/music"
URLBASE = "https://postiz.freio.cz/uploads/"

def psql(sql, tuples=True):
    r = subprocess.run(["docker","exec","postiz-postgres","psql","-U","postiz","-d","postiz","-tAF","\t","-c",sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:600]); sys.exit(1)
    out = [ln for ln in r.stdout.strip().split("\n") if ln]
    return [ln.split("\t") for ln in out] if tuples else out

# budoucí IG stories, které jsou ještě obrázek (png/jpg/jpeg) -> převést
stories = psql("""
  select p.id, i.name, p.image::jsonb->0->>'path'
  from "Post" p join "Integration" i on i.id=p."integrationId"
  where i."providerIdentifier"='instagram' and (p.settings::jsonb->>'post_type')='story'
    and p.state='QUEUE' and p."publishDate">now() and p."deletedAt" is null
    and (p.image::text ilike '%.png%' or p.image::text ilike '%.jpg%' or p.image::text ilike '%.jpeg%')
  order by i.name, p."publishDate";""")

if not stories:
    print("Nic nového: žádná obrázková story bez hudby."); sys.exit(0)

print(f"Nových obrázkových stories k převodu: {len(stories)}")
idx = {}
done = 0
for pid, brand, path in stories:
    if "/uploads/" not in (path or ""):
        print(f"  skip {pid} (bez cesty)"); continue
    rel = path.split("/uploads/", 1)[1]
    d = os.path.dirname(rel)
    newname = secrets.token_hex(16) + ".mp4"
    newrel = f"{d}/{newname}"
    i = idx.get(brand, secrets.randbelow(5)); idx[brand] = i + 1
    track = TRACKS[i % 5]
    off = random.Random(pid).randint(0, 55)
    cmd = ["docker","run","--rm","-v",f"{VOL}:/uploads","-v",f"{MUSICDIR}:/music",
           "--entrypoint","ffmpeg","linuxserver/ffmpeg","-y",
           "-loop","1","-i",f"/uploads/{rel}",
           "-ss",str(off),"-i",f"/music/{track}.mp3","-t","7",
           "-c:v","libx264","-pix_fmt","yuv420p","-r","30",
           "-vf","scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
           "-c:a","aac","-b:a","160k","-af","afade=t=in:d=0.5,afade=t=out:st=6:d=1",
           f"/uploads/{newrel}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(f"   FFMPEG ERR {pid}:", r.stderr[-300:]); continue
    newurl = URLBASE + newrel
    newid = secrets.token_hex(16)
    imgjson = json.dumps([{"id": newid, "path": newurl}])
    psql(f"""update "Post" set image='{imgjson}' where id='{pid}';""", tuples=False)
    print(f"  {brand:26s} {pid} -> {track} @ {off}s")
    done += 1

print(f"Prevedeno stories: {done}")
