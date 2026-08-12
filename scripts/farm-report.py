#!/usr/bin/env python3
"""
farm-report.py — týdenní přehled o agent-farm: jede správně? vyrábí něco k něčemu?

Dvě otázky, které se nedají zodpovědět jednou metrikou:

  PROVOZ  — jestli stroj běží zdravě. Sleduje se poměr pokusů k výsledkům, protože
            právě ten odhalil churn v srpnu (21 726 pokusů, 3 hotové úkoly za den).
            Samotný počet pokusů vypadá jako píle, ale je to spálený rozpočet.

  KVALITA — co reálně vypadlo ven. Bere verdikty z farm_pr_reviews (viz
            farm-pr-review.py) a hlídá i to, kolik PR leží neposbíraných —
            hromada otevřených PR znamená, že farma tvoří rychleji, než se sklízí.

Digest jde na Telegram jen když je co říct (problém nebo týdenní souhrn), ne pokaždé.
Použití: farm-report.py [--days 7] [--quiet]   (--quiet = jen Telegram, bez výpisu)
"""
import json, os, subprocess, sys, urllib.request

DAYS = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--days=")), "7"))
QUIET = "--quiet" in sys.argv

TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN and os.environ.get("TELEGRAM_TOKEN_FILE"):
    try:
        TG_TOKEN = open(os.environ["TELEGRAM_TOKEN_FILE"]).read().strip()
    except Exception:
        pass


def q(sql, db, container):
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "postgres" if "supabase" in container else "postiz",
         "-d", db, "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return []
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def farm(sql):
    """Farma má vlastní Supabase Postgres, insights sedí u Postizu."""
    return q(sql, "postgres", "agentfarm-supabase-db-1")


def ins(sql):
    return q(sql, "insights", "postiz-postgres")


def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


out, alerts = [], []

# --- PROVOZ ----------------------------------------------------------------
projects = farm("select status, count(*) from projects group by status order by 2 desc;")
out.append("PROJEKTY: " + ", ".join(f"{s}={n}" for s, n in projects) if projects
           else "PROJEKTY: nezjištěno")

tasks = farm(f"""select status, count(*) from tasks
                 where created_at > now() - interval '{DAYS} days' group by status order by 2 desc;""")
by_state = {s: int(n) for s, n in tasks}
done = by_state.get("merged", 0) + by_state.get("done", 0)
total = sum(by_state.values())
out.append(f"ÚKOLY za {DAYS} dní: celkem {total}, hotových {done}, "
           + ", ".join(f"{s}={n}" for s, n in tasks if s not in ("merged", "done")))

# Poměr pokusů k hotovým úkolům je hlavní ukazatel churnu. Zdravá farma dělá
# jednotky pokusů na úkol; stovky znamenají, že se práce zahazuje a platí znovu.
# Churn se MUSÍ počítat z krátkého okna. Po opravě churnu 11. 8. by týdenní průměr
# ještě dlouho tahaly předfixové dny (10. 8.: 21 730 pokusů / 3 úspěchy) a alert by
# křičel na problém, který už neexistuje.
att = farm(f"""select count(*) from attempts where started_at > now() - interval '{DAYS} days';""")
d1 = farm("""select count(*), count(*) filter (where status='succeeded')
              from attempts where started_at > now() - interval '24 hours';""")
n_att = int(att[0][0]) if att else 0
a24, ok24 = (int(d1[0][0]), int(d1[0][1])) if d1 else (0, 0)
r24 = a24 / ok24 if ok24 else float("inf")
out.append(f"POKUSY: {n_att} za {DAYS} dní; za 24 h {a24} pokusů → {ok24} úspěšných"
           + (f" ({r24:.0f}:1)" if ok24 else ""))
if ok24 and r24 > 40:
    alerts.append(f"churn: {r24:.0f} pokusů na jeden úspěch za 24 h (zdravé je do ~20)")
if a24 > 200 and not ok24:
    alerts.append(f"za 24 h {a24} pokusů a ani jeden úspěšný")
if total and not done:
    alerts.append(f"za {DAYS} dní {total} úkolů a ani jeden dokončený")

# Fronta (`queued`) roste schválně — zásobník práce. Za zaseknuté se počítá jen to,
# co se TVÁŘÍ, že běží, ale dlouho se nehnulo.
stuck = farm("""select count(*) from tasks
                where status = 'running' and updated_at < now() - interval '6 hours';""")
if stuck and int(stuck[0][0]) > 0:
    alerts.append(f"{stuck[0][0]} úkolů se tváří jako běžící, ale 6+ hodin se nehnulo")

backlog = farm("select count(*) from tasks where status='queued';")
parked = farm("select count(*) from tasks where status='parked';")
out.append(f"FRONTA: {backlog[0][0] if backlog else '?'} čeká, "
           f"{parked[0][0] if parked else '?'} odloženo")

# --- KVALITA ---------------------------------------------------------------
# Bere se jen POSLEDNÍ posudek každého PR — starší snímky by výsledek ředily.
verd = ins("""
  select verdict, count(*) from (
    select distinct on (repo, pr) repo, pr, verdict
    from farm_pr_reviews order by repo, pr, reviewed_at desc) t
  group by verdict order by 2 desc;""")
if verd:
    vd = {v: int(n) for v, n in verd}
    tot = sum(vd.values())
    good = vd.get("useful", 0)
    out.append(f"KVALITA PR: {good}/{tot} užitečných, {vd.get('weak',0)} slabých, "
               f"{vd.get('harmful',0)} škodlivých")
    if vd.get("harmful"):
        h = ins("""select repo, pr, summary from (
                     select distinct on (repo, pr) repo, pr, verdict, summary, reviewed_at
                     from farm_pr_reviews order by repo, pr, reviewed_at desc) t
                   where verdict='harmful' order by repo, pr;""")
        alerts.append("škodlivé PR: " + "; ".join(f"{r}#{p} {s[:70]}" for r, p, s in h[:5]))
    if tot >= 5 and good / tot < 0.4:
        alerts.append(f"jen {good} z {tot} PR je užitečných — zadání pro farmu jsou nejspíš mimo")

    old = ins("""select count(*) from (
                   select distinct on (repo, pr) repo, pr, age_days
                   from farm_pr_reviews order by repo, pr, reviewed_at desc) t
                 where age_days > 14;""")
    if old and int(old[0][0]) > 0:
        out.append(f"NESKLIZENO: {old[0][0]} PR starších 14 dní")
else:
    out.append("KVALITA PR: zatím bez posudků (spusť farm-pr-review.py)")

text = "🚜 Přehled agent-farm\n" + "\n".join(out)
if alerts:
    text += "\n\n⚠️ K řešení:\n" + "\n".join("• " + a for a in alerts)

if not QUIET:
    print(text)
if alerts or "--digest" in sys.argv:
    tg(text)
