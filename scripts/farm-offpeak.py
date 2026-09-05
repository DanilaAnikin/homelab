#!/usr/bin/env python3
"""
farm-offpeak.py — nechá farmu běžet jen v levných hodinách DeepSeeku.

Od 16. 8. 2026 (16:00 UTC) má DeepSeek dvojí sazbu a ve špičce je všechno
přesně 2× dražší. Farma jede stejně nepřetržitě, takže není důvod, aby jela
zrovna v drahých hodinách — přesunout práci mimo ně je 50% úspora zadarmo.

ŠPIČKA (dle DeepSeeku): 01:00–04:00 a 06:00–10:00 UTC.

SOUBĚH S HLÍDAČEM KREDITU: oba dva umí farmu zastavit, takže si musí umět
nešlapat po sobě. Každý si pauzu značkuje do `farm_settings.pause_source` a
ODPAUZOVAT smí jen tu svoji. Kdyby kredit došel během levných hodin, zastaví to
hlídač kreditu; tenhle skript pak na konci špičky farmu nepustí, protože pauza
není jeho — a nemá ji rušit, dokud kredit nedorazí.

Použití: farm-offpeak.py [--dry-run]
"""
import json, subprocess, sys, datetime

DRY = "--dry-run" in sys.argv
FARM_DB = "agentfarm-supabase-db-1"
MARK = "offpeak"

# Půlotevřené intervaly [od, do) v hodinách UTC.
PEAK = [(1, 4), (6, 10)]


def sql(q):
    r = subprocess.run(
        ["sudo", "-n", "docker", "exec", "-i", FARM_DB, "psql", "-U", "postgres",
         "-d", "postgres", "-tAF", "\t", "-c", q],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("DB chyba:", r.stderr[:200]); sys.exit(1)
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def get(key, fallback=None):
    rows = sql(f"select value from farm_settings where key = '{key}';")
    if not rows:
        return fallback
    try:
        return json.loads(rows[0][0])
    except Exception:
        return rows[0][0]


def _vlastnik_zastavil():
    """Zastavil farmu člověk? Jeho pauzu žádný hlídač rušit nesmí.

    `owner_pause` je samostatný klíč právě proto, aby se nedal splést s provozní
    pauzou hlídačů. Ti si svou vlastní poznají podle `pause_source`, jenže tu
    aplikace (dashboard, /kill) nikdy nezapisovala — a tak se stávalo, že hlídač
    zrušil pauzu, kterou nezpůsobil.
    """
    return bool(get("owner_pause", False))


def put(key, value):
    v = json.dumps(value).replace("'", "''")
    sql(f"""insert into farm_settings (key, value) values ('{key}', '{v}'::jsonb)
            on conflict (key) do update set value = excluded.value;""")


now = datetime.datetime.now(datetime.timezone.utc)
in_peak = any(a <= now.hour < b for a, b in PEAK)
paused = bool(get("global_pause", False))
source = get("pause_source", None)

print(f"{now:%H:%M} UTC — {'ŠPIČKA' if in_peak else 'levné hodiny'}; "
      f"farma {'zastavena' if paused else 'běží'}"
      + (f" (zastavil: {source})" if paused and source else ""))

if in_peak and not paused:
    if DRY:
        print("[náhled] zastavil bych farmu na dobu špičky"); sys.exit(0)
    put("global_pause", True)
    put("pause_source", MARK)
    print("→ zastaveno na dobu špičky")

elif not in_peak and paused and source == MARK:
    if DRY:
        print("[náhled] pustil bych farmu"); sys.exit(0)
    if _vlastnik_zastavil():
        print("farmu zastavil člověk (owner_pause) — nepouštím ji")
        sys.exit(0)
    put("global_pause", False)
    put("pause_source", None)
    print("→ špička skončila, farma jede")

elif paused and source != MARK:
    # Typicky došlý kredit. Pustit ji tady by hlídači kreditu přepsalo rozhodnutí.
    print("pauza patří někomu jinému — nesahám na to")
