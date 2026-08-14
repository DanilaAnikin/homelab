#!/usr/bin/env python3
"""
farm-credit-guard.py — zastaví farmu, když dojde kredit u poskytovatele modelů,
a sama ji zase pustí, až kredit je.

PROČ: 14. 8. v 01:09 došel kredit na OpenCode i na záložním DeepSeeku. Farma to
nijak nepoznala a dál dispatchovala — za den 418 pokusů, 0 úspěchů. Hořela jádra,
vznikaly kontejnery, rostla fronta, plnil se disk. Nikdo se to nedozvěděl, protože
health-guard hlídá churn a fan-out, ne peníze, a týdenní přehled běží až v pondělí.

Hlídá se PŘÍMOU ZKOUŠKOU, ne čtením logů: pošle se nejlevnější možný dotaz přes
LiteLLM. To odpoví na otázku „jde to teď?" spolehlivěji než hledání hlášek, které
se s každou verzí knihovny mění.

BEZPEČNOST: skript nikdy nerozjede farmu, kterou zastavil člověk. Pauzu si značkuje
(`pause_source`) a odpauzuje jen tu vlastní.

Použití: farm-credit-guard.py [--dry-run]
"""
import json, os, re, subprocess, sys, time, urllib.request

DRY = "--dry-run" in sys.argv
FARM_DB = "agentfarm-supabase-db-1"
ENV_PATH = "/srv/homelab/compose/agent-farm/app/.env"
PROBE_MODEL = "cheap"  # nejlevnější skupina; stačí ověřit, že projde autorizace
MARK = "credit_guard"


def sudo_read(path):
    r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, text=True, timeout=20)
    return r.stdout or ""


ENV = {}
for ln in sudo_read(ENV_PATH).splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        ENV[k.strip()] = v.strip().strip("\"'")

BASE = (ENV.get("LITELLM_BASE_URL") or "http://127.0.0.1:4000").replace("litellm:", "127.0.0.1:")
KEY = ENV.get("LITELLM_MASTER_KEY") or ""

TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN and os.environ.get("TELEGRAM_TOKEN_FILE"):
    try:
        TG_TOKEN = open(os.environ["TELEGRAM_TOKEN_FILE"]).read().strip()
    except Exception:
        TG_TOKEN = sudo_read(os.environ["TELEGRAM_TOKEN_FILE"]).strip() or None


def sql(q):
    r = subprocess.run(
        ["sudo", "-n", "docker", "exec", "-i", FARM_DB, "psql", "-U", "postgres",
         "-d", "postgres", "-tAF", "\t", "-c", q],
        capture_output=True, text=True, timeout=60)
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


def probe():
    """Vrací (ok, popis). Rozlišuje 'došly peníze' od 'služba je dole' — první
    znamená zastavit a říct o tom člověku, druhé přejde samo."""
    body = json.dumps({
        "model": PROBE_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        f"{BASE.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        return True, "poskytovatel odpovídá"
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try: detail = e.read().decode()[:800]
            except Exception: pass
        blob = f"{e} {detail}"
        if re.search(r"insufficient\s*balance|exceeded your current quota|billing", blob, re.I):
            return False, "došel kredit u poskytovatele modelů"
        if re.search(r"401|403|authenticationerror|invalid api key", blob, re.I):
            return False, "poskytovatel odmítá klíč (401/403)"
        return None, f"nedostupné: {str(e)[:80]}"  # None = neurčité, nezasahuj


def get(key, fallback=None):
    rows = sql(f"select value from farm_settings where key = '{key}';")
    if not rows:
        return fallback
    try:
        return json.loads(rows[0][0])
    except Exception:
        return rows[0][0]


def put(key, value):
    v = json.dumps(value).replace("'", "''")
    sql(f"""insert into farm_settings (key, value) values ('{key}', '{v}'::jsonb)
            on conflict (key) do update set value = excluded.value;""")


def probe_with_retry():
    """Farma bije do LiteLLM tak hustě, že zkouška dostane 429 a nic nezjistí.
    Proto se opakuje, a když ani pak nic, rozhodne se z chování samotné farmy:
    hodně pokusů a nula úspěchů za poslední hodinu znamená, že modely nejedou —
    ať už kvůli penězům, nebo klíči. Obojí je důvod zastavit."""
    for i in range(3):
        ok, why = probe()
        if ok is not None:
            return ok, why
        if i < 2:
            time.sleep(20)

    rows = sql("""select count(*), count(*) filter (where status = 'succeeded')
                  from attempts where started_at > now() - interval '60 minutes';""")
    tried, good = (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)
    if tried >= 20 and good == 0:
        return False, (f"zkouška se nedovolala ({why}), ale farma za hodinu udělala "
                       f"{tried} pokusů a ani jeden neuspěl — modely nejedou")
    return None, why


ok, why = probe_with_retry()
paused = bool(get("global_pause", False))
source = get("pause_source", None)

print(f"zkouška: {why} | farma {'ZASTAVENA' if paused else 'běží'}"
      + (f" (zastavil: {source})" if paused and source else ""))

if ok is None:
    print("neurčitý výsledek — nezasahuji")  # výpadek sítě farmu vypínat nemá
    sys.exit(0)

if not ok and not paused:
    if DRY:
        print("[náhled] zastavil bych farmu"); sys.exit(0)
    put("global_pause", True)
    put("pause_source", MARK)
    print("→ farma zastavena")
    tg(f"🛑 Farma zastavena: {why}.\n\n"
       f"Dispatch by jinak dál běžel naprázdno — 14. 8. to takhle stálo 418 pokusů "
       f"a 0 úspěchů za den. Až kredit doplníš, hlídač farmu do 10 minut sám pustí.")

elif ok and paused and source == MARK:
    if DRY:
        print("[náhled] pustil bych farmu"); sys.exit(0)
    put("global_pause", False)
    put("pause_source", None)
    print("→ farma zase běží")
    tg("✅ Kredit je zpět, farma znovu spuštěna.")

elif ok and paused:
    print("farmu zastavil někdo jiný — nesahám na to")
