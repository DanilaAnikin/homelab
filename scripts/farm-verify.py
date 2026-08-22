#!/usr/bin/env python3
"""
Jednorázové ověření, že farma po opravách 21.–22. 8. reálně pracuje.

Spouští se sama ~40 minut po konci špičky. Neptá se „běží něco?", ale jestli
pokusy DOBÍHAJÍ K SOUDCI a jestli se nevrací ty konkrétní poruchy, které tomu
předcházely:
  - zaseknutá příprava (running dlouho, steps_used = 0)   → 15h výpadek 22. 8.
  - fetch failed                                          → zabíjení živých pokusů
  - fan-out fronty                                        → duplicitní práce
Výsledek jde na Telegram, ať ho majitel má i bez toho, aby se někam díval.
"""
import json, os, subprocess, urllib.request

DB = ["sudo", "-n", "docker", "exec", "-i", "agentfarm-supabase-db-1",
      "psql", "-U", "postgres", "-d", "postgres", "-tAF", "\t", "-c"]


def q(sql):
    r = subprocess.run(DB + [sql], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return []
    return [l.split("\t") for l in r.stdout.strip().split("\n") if l]


def one(sql, default="0"):
    rows = q(sql)
    return rows[0][0].strip() if rows and rows[0] else default


att = q("""select status, count(*), coalesce(round(avg(steps_used)),0)
           from attempts where started_at > now() - interval '3 hours'
           group by 1 order by 2 desc;""")
stuck = one("""select count(*) from attempts where status='running'
               and steps_used = 0 and started_at < now() - interval '45 minutes';""")
fetchfail = one("""select count(*) from attempts where started_at > now() - interval '3 hours'
                   and output_summary like '%fetch failed%';""")
fanout = one("""select coalesce(max(cnt),0) from
                (select count(*) cnt from pgmq.q_q_tasks group by message->>'taskId') s;""")
tasks = q("""select t.status, count(*) from tasks t join projects p on p.id=t.project_id
             where p.status='active' group by 1 order by 2 desc;""")
spend = subprocess.run(
    ["sudo", "-n", "docker", "exec", "-i", "agentfarm-supabase-db-1", "psql", "-U", "postgres",
     "-d", "litellm", "-tAc",
     "select round(coalesce(sum(spend),0)::numeric,3) from \"LiteLLM_SpendLogs\" "
     "where \"startTime\" >= date_trunc('day', now());"],
    capture_output=True, text=True, timeout=60).stdout.strip()

total = sum(int(a[1]) for a in att) if att else 0
judged = sum(int(a[1]) for a in att if a[0] in ("rejected", "succeeded")) if att else 0

lines = [f"pokusy za 3 h: {total or 'ŽÁDNÝ'}"]
for s, n, k in att:
    lines.append(f"  {s}: {n} (prům. {k} kroků)")
lines.append(f"úkoly: " + ", ".join(f"{s}={n}" for s, n in tasks))
lines.append(f"utraceno dnes: ${spend}")

# Pozastavená farma nedělá pokusy schválně — hlásit to jako poruchu je přesně ten
# falešný poplach, kvůli kterému se pak přehlédne skutečný problém.
paused = one("select coalesce((select value::text from farm_settings "
             "where key='global_pause'),'false');", "false") == "true"
psrc = one("select coalesce((select value::text from farm_settings "
           "where key='pause_source'),'?');", "?").strip('"')

bad = []
if int(stuck) > 0:
    bad.append(f"{stuck} pokusů visí v přípravě s 0 kroky — vrátila se porucha z 22. 8.")
if int(fetchfail) > 0:
    bad.append(f"{fetchfail}× fetch failed — vrátilo se zabíjení živých pokusů")
if int(fanout) > 1:
    bad.append(f"fan-out fronty {fanout}:1")
if total == 0 and not paused:
    bad.append("za 3 hodiny ani jeden pokus")

if bad:
    head = "⚠️ Farma po opravách NEJEDE, jak má"
elif paused and total == 0:
    head = f"ℹ️ Farma je pozastavená ({psrc}) — není co měřit"
elif judged > 0:
    head = f"✅ Farma jede — {judged} pokusů dorazilo k soudci, žádná ze starých poruch"
else:
    head = "ℹ️ Farma běží, ale zatím nic nedorazilo k soudci"

text = head + "\n\n" + "\n".join(lines) + (("\n\nK řešení:\n• " + "\n• ".join(bad)) if bad else "")
print(text)

tok_file = os.environ.get("TELEGRAM_TOKEN_FILE")
tok = os.environ.get("TELEGRAM_BOT_TOKEN")
if not tok and tok_file:
    tok = subprocess.run(["sudo", "-n", "cat", tok_file], capture_output=True,
                         text=True, timeout=20).stdout.strip()
chat = os.environ.get("TELEGRAM_CHAT_ID")
if tok and chat:
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text[:3800]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
        print("\n(odesláno na Telegram)")
    except Exception as e:
        print("\nTelegram selhal:", str(e)[:80])
