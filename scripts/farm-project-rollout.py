#!/usr/bin/env python3
"""
farm-project-rollout.py — postupné, automatické zapínání projektů agent-farmy.

Projekty čekají ve stavu 'stopped'. Tenhle skript při každém běhu ověří ZDRAVÍ
serveru i farmy a když je všechno v pořádku, převede DO 'active' právě JEDEN
další projekt. Nic víc. Když je něco špatně, jen počká na další běh.

Proč vůbec: worker cap (MAX_WORKERS_TOTAL) omezuje zátěž globálně, takže víc
projektů NEznamená víc souběžné práce — jen se stejný výkon rozdělí mezi víc
projektů. Rizika, která s počtem projektů ROSTOU, hlídají brány níž (disk,
churn, útrata). Postupné zapínání navíc dá čas si všimnout, když se něco pokazí.

Použití:
  farm-project-rollout.py            # vyhodnotí brány, případně zapne 1 projekt
  farm-project-rollout.py --dry-run  # jen ukáže rozhodnutí
"""
import os, re, shutil, subprocess, sys

DRY = "--dry-run" in sys.argv

# --- Prahové hodnoty bran -----------------------------------------------------
MIN_DISK_FREE_GB = 150      # workspaces rostou ~1,6 GB/task; GC běží denně
MIN_RAM_FREE_GB = 4.0       # available (ne free) — cache se uvolní
MAX_LOAD15 = 6.0            # 8 jader
MAX_FAILED_6H = 150         # churn: zdravě jsou desítky, rozbitě tisíce
MAX_INSTANT_FAILED_6H = 60  # pokusy padající do 5 s = typický churn
SPEND_HEADROOM = 0.85       # útrata musí být pod 85 % denního stropu
SOAK_HOURS = 12             # po zapnutí projektu počkej (2 čisté 6h churn okna), než přidáš další


def psql(sql):
    r = subprocess.run(
        ["docker", "exec", "-i", "agentfarm-supabase-db-1",
         "psql", "-U", "supabase_admin", "-d", "postgres", "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"CHYBA psql: {r.stderr[:300]}"); sys.exit(1)
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def one(sql, default="0"):
    rows = psql(sql)
    return rows[0][0] if rows and rows[0] and rows[0][0] != "" else default


gates, blocked = [], []


def gate(name, ok, detail):
    (gates if ok else blocked).append(f"{'✓' if ok else '✗'} {name}: {detail}")


# --- 1) Disk ------------------------------------------------------------------
free_gb = shutil.disk_usage("/").free / 2**30
gate("disk", free_gb >= MIN_DISK_FREE_GB, f"{free_gb:.0f} GB volných (min {MIN_DISK_FREE_GB})")

# --- 2) RAM (MemAvailable) ----------------------------------------------------
avail_gb = 0.0
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                avail_gb = int(line.split()[1]) / 2**20
                break
except OSError:
    pass
gate("RAM", avail_gb >= MIN_RAM_FREE_GB, f"{avail_gb:.1f} GB dostupných (min {MIN_RAM_FREE_GB})")

# --- 3) Load ------------------------------------------------------------------
load15 = os.getloadavg()[2]
gate("load", load15 <= MAX_LOAD15, f"{load15:.1f} (max {MAX_LOAD15}, 8 jader)")

# --- 4) Churn — farma se nesmí točit v chybové smyčce -------------------------
failed6 = int(one("select count(*) from attempts where started_at > now() - interval '6 hours' and status='failed';"))
instant6 = int(one("select count(*) from attempts where started_at > now() - interval '6 hours' and status='failed' and wall_ms < 5000;"))
gate("churn", failed6 <= MAX_FAILED_6H, f"{failed6} selhání/6 h (max {MAX_FAILED_6H})")
gate("churn-instant", instant6 <= MAX_INSTANT_FAILED_6H,
     f"{instant6} okamžitých selhání/6 h (max {MAX_INSTANT_FAILED_6H})")

# --- 5) Útrata pod stropem ----------------------------------------------------
spend = float(one("select coalesce(sum(cost_usd),0) from cost_ledger where ts::date = current_date;"))
cap = float(one("select coalesce((caps_override->>'dailyCapUsd')::float, 10) from profiles limit 1;", "10"))
gate("útrata", spend < cap * SPEND_HEADROOM, f"${spend:.2f} z ${cap:.0f}/den (limit {int(SPEND_HEADROOM*100)} %)")

# --- 6) Žádný projekt nevisí v budget_hold ------------------------------------
held = int(one("select count(*) from projects where status='budget_hold';"))
gate("budget_hold", held == 0, f"{held} projektů v budget_hold")

# --- 7) Soak — od posledního zapnutí musí uplynout dost času ------------------
last_h = one("""select coalesce(round(extract(epoch from (now() - max(ts)))/3600)::text,'999')
                from events where type='project_rollout_activated';""", "999")
last_h = float(last_h)
gate("soak", last_h >= SOAK_HOURS, f"{last_h:.0f} h od posledního zapnutí (min {SOAK_HOURS})")

# --- Rozhodnutí ---------------------------------------------------------------
# --- Vypnutá farma znamená vypnutá i tady -------------------------------------
# Tenhle skript volá placený model MIMO orchestrátor i mimo LiteLLM, takže se ho
# `global_pause` sám od sebe netýká. Bez téhle kontroly by po zastavení farmy dál
# utrácel podle svého timeru — přesně to majitel zastavením zakázal.
def _farma_je_vypnuta():
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "agentfarm-supabase-db-1", "psql", "-U", "postgres",
             "-d", "postgres", "-tAc",
             "select value from farm_settings where key = 'global_pause';"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False   # nedostupná DB nesmí skript umlčet natrvalo


if _farma_je_vypnuta():
    print("farma je zastavená (global_pause) — nic nedělám")
    sys.exit(0)
# ------------------------------------------------------------------------------

waiting = psql("select id::text, name from projects where status='stopped' order by created_at limit 1;")
active_n = int(one("select count(*) from projects where status='active';"))
waiting_n = int(one("select count(*) from projects where status='stopped';"))

print(f"agent-farm rollout — aktivních {active_n}, čeká v čekárně {waiting_n}")
for g in gates:
    print("  " + g)
for b in blocked:
    print("  " + b)

if not waiting:
    print("→ Čekárna prázdná, není co zapínat.")
    sys.exit(0)

pid, pname = waiting[0]

if blocked:
    print(f"→ ČEKÁM (brány neprošly). Další na řadě: {pname}")
    sys.exit(0)

if DRY:
    print(f"→ DRY-RUN: zapnul bych projekt {pname}")
    sys.exit(0)

psql(f"update projects set status='active', updated_at=now() where id='{pid}';")
msg = (f"Postupný rollout: projekt zapnut do provozu. "
       f"Volno {free_gb:.0f} GB, RAM {avail_gb:.1f} GB, load {load15:.1f}, "
       f"{failed6} selhání/6 h, útrata ${spend:.2f}/${cap:.0f}.").replace("'", "''")
psql(f"""insert into events (project_id, level, type, message)
         values ('{pid}', 'info', 'project_rollout_activated', '{msg}');""")
print(f"→ ZAPNUTO: {pname} (zbývá v čekárně: {waiting_n - 1})")
