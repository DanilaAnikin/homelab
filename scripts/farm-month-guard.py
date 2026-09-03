#!/usr/bin/env python3
"""
Měsíční strop farmy vynucený proti SKUTEČNÝM penězům.

Proč ne z `cost_ledger`: ten si LiteLLM počítá sám z tokenů × ceníku v
`infra/litellm/config.yaml`, a ten ceník se rozešel se skutečností — od zdražení
16. 8. 2026 u všech skupin chybí cena za cache-hit a výstup je naceněný na
$0,28/M místo $0,66/M. Ledger proto podhodnocuje; srpen ukázal 38,60 USD, zatímco
skutečná útrata vyšla odhadem na ~46 USD. Strop postavený na takovém čísle propustí
o polovinu víc, než má.

Zůstatek u poskytovatele je naopak tvrdý fakt. Klíč používá na tomhle stroji jen
farma, takže každý pokles zůstatku je její útrata.

Měří se přírůstkově: sčítají se jen POKLESY zůstatku. Dobití zůstatek zvedne a
smí posunout referenci, ale nesmí se tvářit jako záporná útrata — jinak by si
farma dobitím „vynulovala" měsíc.

Zastavuje pod vlastní značkou `budget_month`; ostatní hlídači (`credit_guard`,
`offpeak`) odpauzovávají jen tu svou, takže si navzájem nepřekáží. Ruční pauzu
vlastníka (`owner`) nikdy neruší.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

DRY = "--dry-run" in sys.argv
FARM_DB = "agentfarm-supabase-db-1"
ENV_PATH = "/srv/homelab/compose/agent-farm/app/.env"
MARK = "budget_month"
DEFAULT_CAP_USD = 20.0


def sudo_read(path):
    r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, text=True, timeout=20)
    return r.stdout if r.returncode == 0 else ""


ENV = {}
for line in sudo_read(ENV_PATH).splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        ENV[k.strip()] = v.strip().strip('"').strip("'")

TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def sql(q):
    r = subprocess.run(
        ["sudo", "-n", "docker", "exec", "-i", FARM_DB, "psql", "-U", "postgres", "-d", "postgres", "-tAc", q],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"CHYBA: dotaz do DB selhal: {r.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(2)
    return r.stdout.strip()


def get(key, fallback=None):
    out = sql(f"select value from farm_settings where key = '{key}';")
    if not out:
        return fallback
    try:
        return json.loads(out)
    except Exception:
        return fallback


def put(key, value):
    v = json.dumps(value).replace("'", "''")
    sql(
        f"insert into farm_settings (key, value) values ('{key}', '{v}'::jsonb) "
        f"on conflict (key) do update set value = excluded.value, updated_at = now();"
    )


def tg(text):
    if not (TG_CHAT and TG_TOKEN):
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        pass


def balance_usd():
    """Zůstatek u DeepSeeku, nebo None při výpadku sítě (tehdy se NEZASAHUJE)."""
    key = ENV.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=25).read())
    except Exception as e:
        print(f"zůstatek nepřečten ({e}) — nezasahuji", file=sys.stderr)
        return None
    for info in data.get("balance_infos", []):
        if info.get("currency") == "USD":
            try:
                return float(info["total_balance"])
            except (KeyError, ValueError):
                return None
    return None


now = datetime.now(timezone.utc)
month = now.strftime("%Y-%m")
cap = float(get("farm_monthly_cap_usd", DEFAULT_CAP_USD) or DEFAULT_CAP_USD)

current = balance_usd()
if current is None:
    # Výpadek sítě nesmí farmu ani zastavit, ani pustit.
    sys.exit(0)

put("deepseek_balance_usd", current)

stored_month = get("month_guard_month", None)
last = get("month_guard_last_balance", None)
spent = float(get("month_guard_spent_usd", 0) or 0)

if stored_month != month:
    # Nový měsíc: účet se nuluje, referencí je dnešní zůstatek.
    spent = 0.0
    put("month_guard_month", month)
    print(f"nový měsíc {month} — účet vynulován")
elif isinstance(last, (int, float)):
    delta = float(last) - current
    if delta > 0:
        spent += delta          # utraceno
    # delta <= 0 znamená dobití; reference se posune níž, ale útrata se nesnižuje.

put("month_guard_last_balance", current)
put("month_guard_spent_usd", round(spent, 4))

paused = bool(get("global_pause", False))
source = get("pause_source", None)
print(f"{month}: utraceno {spent:.2f} / {cap:.2f} USD | zůstatek {current:.2f} | "
      f"farma {'ZASTAVENA' if paused else 'běží'}" + (f" ({source})" if paused and source else ""))

if spent >= cap and not paused:
    if DRY:
        print("[náhled] zastavil bych farmu"); sys.exit(0)
    put("global_pause", True)
    put("pause_source", MARK)
    print("→ farma zastavena (měsíční strop)")
    tg(f"🛑 Farma zastavena: vyčerpaný měsíční strop.\n\n"
       f"Za {month} utraceno {spent:.2f} USD ze stropu {cap:.2f} USD "
       f"(měřeno poklesem zůstatku u DeepSeeku, ne odhadem z ceníku).\n"
       f"Zbývající zůstatek: {current:.2f} USD.\n\n"
       f"Sama se pustí 1. dne dalšího měsíce. Dřív jen ručně:\n"
       f"update farm_settings set value='false' where key='global_pause';")

elif spent < cap and paused and source == MARK:
    if DRY:
        print("[náhled] pustil bych farmu"); sys.exit(0)
    put("global_pause", False)
    put("pause_source", None)
    print("→ farma zase běží (nový měsíc)")
    tg(f"✅ Nový měsíc — měsíční strop se vynuloval, farma znovu běží. Strop: {cap:.2f} USD.")
