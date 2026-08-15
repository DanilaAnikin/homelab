#!/usr/bin/env python3
"""
farm-opencode-ab.py — změří, jestli je OpenCode Go pro tuhle farmu levnější než
DeepSeek napřímo, a když zjevně není, vrátí to zpátky.

OTÁZKA, KTEROU TO ŘEŠÍ: OpenCode stojí $10/měsíc paušál a slibuje $60 hodnoty.
Přímý DeepSeek vychází na ~$18/měsíc. Papírově je OpenCode levnější — jenže tahle
zátěž je z 94–98 % cache-hit a celá levnost přímého DeepSeeku stojí právě na tom
($0,0028/M místo $0,14/M). Jestli OpenCode tuhle slevu do své kvóty promítá,
nikde nepíšou. Nedá se to odhadnout, jen změřit.

CO SE MĚŘÍ: kolik kvóty ubude na jeden milion protečených tokenů. To se porovná
s efektivní sazbou přímého DeepSeeku spočítanou ze stejného období. Kvóta se čte
z https://opencode.ai/zen/go/v1/usage (procenta z měsíčního stropu $60).

PROČ SNAPSHOTY: endpoint hlásí jen okamžitý stav v procentech. Sazba se dá získat
až z rozdílu dvou měření, takže se sbírá průběžně a vyhodnocuje se z rozdílu.

BEZPEČNOST: když OpenCode vyjde dráž než trojnásobek přímé sazby, skript sám
přepne konfiguraci zpět na přímý DeepSeek a pošle Telegram — jinak by zbytek už
zaplacené měsíční kvóty shořel na testu, který je stejně prohraný.

Použití: farm-opencode-ab.py [--report] [--no-revert]
"""
import json, os, re, subprocess, sys, urllib.request

REPORT = "--report" in sys.argv
NO_REVERT = "--no-revert" in sys.argv

FARM_DB = "agentfarm-supabase-db-1"
LITELLM_DIR = "/srv/homelab/compose/agent-farm/app/infra/litellm"
ENV_PATH = "/srv/homelab/compose/agent-farm/app/.env"
MONTHLY_BUDGET_USD = 60.0   # co OpenCode Go slibuje za $10/měsíc
REVERT_FACTOR = 3.0         # dráž než 3x přímá sazba = test prohrán


def sudo_read(path):
    r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, text=True, timeout=20)
    return r.stdout or ""


ENV = {}
for ln in sudo_read(ENV_PATH).splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        ENV[k.strip()] = v.strip().strip("\"'")

TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN and os.environ.get("TELEGRAM_TOKEN_FILE"):
    TG_TOKEN = (sudo_read(os.environ["TELEGRAM_TOKEN_FILE"]).strip() or None)


def dsql(db, q):
    r = subprocess.run(
        ["sudo", "-n", "docker", "exec", "-i",
         FARM_DB if db != "insights" else "postiz-postgres", "psql",
         "-U", "postgres" if db != "insights" else "postiz", "-d", db,
         "-tAF", "\t", "-c", q], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("DB chyba:", r.stderr[:200]); sys.exit(1)
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print("(Telegram nenakonfigurován)"); return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text[:3800]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception as e:
        print("Telegram selhal:", str(e)[:80])


def quota():
    req = urllib.request.Request(
        "https://opencode.ai/zen/go/v1/usage",
        # Bez vlastního User-Agent vrací OpenCode 403 — výchozí "Python-urllib"
        # jim neprojde přes ochranu.
        headers={"Authorization": f"Bearer {ENV.get('OPENCODE_GO_API_KEY','')}",
                 "User-Agent": "farm-opencode-ab/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["usage"]


def tokens_through(prefix):
    """Kumulativní tokeny přes danou trasu. `openai/` = OpenCode, `deepseek/` = přímo."""
    rows = dsql("litellm", f"""
        select coalesce(sum(total_tokens),0), coalesce(sum(spend),0), count(*)
        from "LiteLLM_SpendLogs" where model like '{prefix}%';""")
    return (int(rows[0][0]), float(rows[0][1]), int(rows[0][2])) if rows else (0, 0.0, 0)


def active_variant():
    cur = sudo_read(f"{LITELLM_DIR}/config.yaml")
    return "opencode" if "opencode.ai/zen/go" in cur else "direct"


def switch_to(variant):
    subprocess.run(["sudo", "-n", "cp", f"{LITELLM_DIR}/config.{variant}.yaml",
                    f"{LITELLM_DIR}/config.yaml"], check=True, timeout=30)
    subprocess.run(["sudo", "-n", "docker", "restart", "agent-farm-litellm-1"],
                   capture_output=True, timeout=120)


dsql("insights", """
create table if not exists farm_opencode_ab (
  id bigserial primary key,
  ts timestamptz not null default now(),
  monthly_pct numeric, weekly_pct numeric, rolling_pct numeric,
  oc_tokens bigint, oc_calls int,
  direct_tokens bigint, direct_spend numeric,
  variant text
);
-- Verdikt se vyhodnocuje každou hodinu, ale oznámit se má JEDNOU. Bez tohohle by
-- Telegram dostával tutéž zprávu pořád dokola.
create table if not exists farm_opencode_ab_alerts (
  verdict text primary key,
  ts timestamptz not null default now()
);""")


def alert_once(kind, text):
    """Pošle na Telegram jen při prvním výskytu daného verdiktu."""
    rows = dsql("insights", f"""insert into farm_opencode_ab_alerts (verdict)
                                values ('{kind}') on conflict do nothing returning verdict;""")
    if rows:
        tg(text)
    else:
        print("(verdikt už byl jednou oznámen, Telegram přeskočen)")

u = quota()
oc_tok, _, oc_calls = tokens_through("openai/")
dir_tok, dir_spend, _ = tokens_through("deepseek/")
variant = active_variant()

dsql("insights", f"""insert into farm_opencode_ab
  (monthly_pct, weekly_pct, rolling_pct, oc_tokens, oc_calls, direct_tokens, direct_spend, variant)
  values ({u['monthly']['percent']}, {u['weekly']['percent']}, {u['rolling']['percent']},
          {oc_tok}, {oc_calls}, {dir_tok}, {dir_spend}, '{variant}');""")

print(f"varianta: {variant} | kvóta OpenCode — měsíc {u['monthly']['percent']} %, "
      f"týden {u['weekly']['percent']} % ({u['weekly']['status']}), 5h {u['rolling']['percent']} %")
print(f"tokeny: přes OpenCode {oc_tok:,}, přímo {dir_tok:,}")

# --- vyhodnocení z rozdílu dvou měření -------------------------------------
rows = dsql("insights", """
  select monthly_pct, oc_tokens, direct_tokens, direct_spend
  from farm_opencode_ab order by ts asc limit 1;""")
if not rows:
    sys.exit(0)
first_pct, first_oc, first_dir, first_spend = (
    float(rows[0][0]), int(rows[0][1]), int(rows[0][2]), float(rows[0][3]))

d_pct = float(u["monthly"]["percent"]) - first_pct
d_oc = oc_tok - first_oc
d_dir = dir_tok - first_dir
d_spend = dir_spend - first_spend

dir_rate = (d_spend / (d_dir / 1e6)) if d_dir > 1_000_000 else None

# POZOR na granularitu: kvóta se hlásí v CELÝCH procentech, tedy po $0,60. Kdyby se
# sazba počítala z rozdílu 0 %, vyšla by $0/M a skript by vyhlásil OpenCode jako
# levnější, aniž by cokoli změřil. Proto dva režimy:
#   - ubyla aspoň 2 % → sazba se dá spočítat přímo,
#   - neubylo skoro nic, ale proteklo hodně tokenů → to je samo o sobě důkaz, že je
#     levný; sazba se pak uvádí jako HORNÍ ODHAD, ne jako změřená hodnota.
MIN_PCT = 2
MIN_TOKENS_FOR_UPPER_BOUND = 50_000_000

if d_pct >= MIN_PCT and d_oc >= 2_000_000:
    oc_rate = (MONTHLY_BUDGET_USD * d_pct / 100) / (d_oc / 1e6)
    bound = ""
elif d_oc >= MIN_TOKENS_FOR_UPPER_BOUND:
    oc_rate = (MONTHLY_BUDGET_USD * MIN_PCT / 100) / (d_oc / 1e6)
    bound = "nejvýše "
else:
    print(f"zatím málo dat — přes OpenCode proteklo {d_oc:,} tokenů a ubylo {d_pct:.0f} % kvóty "
          f"(potřeba {MIN_PCT} % nebo {MIN_TOKENS_FOR_UPPER_BOUND//1_000_000} M tokenů). "
          f"Týdenní kvóta se resetuje {u['weekly']['resetsAt'][:10]}.")
    sys.exit(0)

msg = [f"OpenCode: {bound}${oc_rate:.4f} za 1M tokenů (ubylo {d_pct:.0f} % měsíční kvóty na {d_oc/1e6:.1f} M tokenů)"]
if dir_rate:
    msg.append(f"DeepSeek napřímo: ${dir_rate:.4f} za 1M tokenů")
    msg.append(f"poměr: OpenCode je {oc_rate/dir_rate:.1f}× "
               + ("dražší" if oc_rate > dir_rate else "levnější"))
print("\n".join(msg))

if dir_rate and oc_rate > dir_rate * REVERT_FACTOR:
    verdict = (f"❌ OpenCode Go se pro tuhle zátěž nevyplatí: ${oc_rate:.4f}/M proti "
               f"${dir_rate:.4f}/M napřímo ({oc_rate/dir_rate:.1f}×). Cache-hit slevu "
               f"do kvóty zjevně nepromítá.")
    if variant == "opencode" and not NO_REVERT:
        switch_to("direct")
        verdict += "\n\nVráceno na přímý DeepSeek, zbytek měsíční kvóty zůstal nedotčený."
    print(verdict); alert_once("harmful", "🧪 " + verdict)
elif dir_rate and oc_rate < dir_rate:
    good = (f"✅ OpenCode Go vychází levněji: ${oc_rate:.4f}/M proti ${dir_rate:.4f}/M napřímo. "
            f"Při $10 paušálu měsíčně se vyplatí ho nechat.")
    print(good)
    alert_once("cheaper", "🧪 " + good)
elif REPORT:
    tg("🧪 Test OpenCode:\n" + "\n".join(msg))
