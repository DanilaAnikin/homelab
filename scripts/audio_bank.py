#!/usr/bin/env python3
# Fáze A: natáhne banku licencovaných tracků z Meta ig_audio pro každý IG účet.
# Běží na homelabu; DB přes `docker exec postiz-postgres psql`, Graph API přes urllib.
import json, subprocess, urllib.request, urllib.parse, sys

def psql(sql):
    r = subprocess.run(["sudo","docker","exec","postiz-postgres","psql","-U","postiz","-d","postiz","-tAF","\t","-c",sql],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:500]); sys.exit(1)
    return [ln for ln in r.stdout.strip().split("\n") if ln]

def graph(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"__err": str(e)}

# IG účty: internalId, name, token
rows = psql("""select "internalId", name, token from "Integration"
              where "providerIdentifier"='instagram' and not disabled order by name;""")

SEARCHES = ["", "happy", "chill", "energetic", "upbeat", "corporate", "lofi", "cinematic", "inspiring", "fun"]
bank = {}
for r in rows:
    iid, name, token = r.split("\t")
    parts = token.split("___")
    use = parts[1] if len(parts) > 1 and parts[1] else parts[0]
    seen = {}
    for q in SEARCHES:
        qp = f"&search_query={urllib.parse.quote(q)}" if q else ""
        data = graph(f"https://graph.facebook.com/v22.0/ig_audio?audio_type=music&user_id={iid}{qp}&access_token={use}")
        for a in (data.get("audio") or []):
            aid = a.get("audio_id")
            if aid and aid not in seen:
                seen[aid] = {"id": aid, "title": a.get("title",""), "artist": a.get("display_artist") or a.get("ig_username","")}
    bank[iid] = {"name": name, "tracks": list(seen.values())}
    print(f"{name:28s} internalId={iid}  tracků={len(seen)}")

with open("/srv/homelab/scripts/postiz-audio-bank.json","w") as f:
    json.dump(bank, f, ensure_ascii=False, indent=1)
print("\nUloženo do /srv/homelab/scripts/postiz-audio-bank.json")
print("Celkem unikátních tracků:", sum(len(v["tracks"]) for v in bank.values()))
