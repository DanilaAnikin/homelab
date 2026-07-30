#!/usr/bin/env python3
"""Self-healing poller: TŘI zdroje incidentů → respond.sh (dedup + cooldown).

  1) Uptime Kuma DOWN monitory (web/DB/infra dostupnost).
  2) Prometheus FIRING alerty (disk/crashloop/cert/pamět/postgres) — actionable třídy.
  3) Loki chybové SPIKY v logách appek (panic/fatal/OOM/too-many-connections/5xx…) —
     spike-detekce přes EWMA baseline, ať steady-state log-šum netriggeruje agenta.

Prometheus/Loki nejsou publikované na host (Swarm overlay), čtou se přes `docker exec
obs-prometheus wget` (obs-prometheus má wget a je na stejné síti jako obs-loki).

Robustní: každý zdroj má vlastní try/except (jeden padlý neoslepí ostatní) a po N
po sobě jdoucích chybách pollu eskaluje na Telegram + do incidents.log.
"""
import subprocess, time, json, urllib.parse

KUMA = "http://localhost:3001"; USER = "DanilaAnikin"
COOLDOWN = 1800           # 30 min per incident-key
ERROR_ESCALATE_AFTER = 5  # po tolika chybách pollu eskaluj (~7.5 min)
INCIDENTS_LOG = "/srv/homelab/self-healing/incidents.log"
NOTIFY = "/srv/homelab/self-healing/notify.sh"
RESPOND = "/srv/homelab/self-healing/respond.sh"
PROM_CONTAINER = "obs-prometheus"
ALIVE_PUSH_FILE = "/srv/homelab/secrets/kuma-selfheal-push-url.txt"

ACTIONABLE = {
    "DiskDochazi", "KontejnerSeRestartuje", "PostgresNedostupny",
    "MaloPameti", "KontejnerZeraPamet", "PostgresDochazejiSpojeni",
    "CertifikatBrzyVyprsi",
}

# Loki: závažné vzory (bez literálních závorek → čistý RE2 regex, žádné escape peklo).
LOKI_PATTERN = ("(?i)(panic|fatal|oomkill|out of memory|segfault|too many connections|"
                "connection refused|internal server error|service unavailable|"
                "unhandledrejection|traceback)")
LOKI_QUERY = (f'sum(count_over_time({{container=~".+"}} |~ "{LOKI_PATTERN}" [5m])) by (container)')
LOKI_SEVERE_MIN = 15      # min. absolutní počet za 5m, aby vůbec šlo o incident
LOKI_SPIKE_MULT = 3.0     # a zároveň > 3× klouzavý baseline (spike, ne steady-state)


def kuma_pass():
    for line in open("/srv/homelab/secrets/kuma-login.txt"):
        if line.startswith("KUMA_PASS="):
            return line.split("=", 1)[1].strip()


PASS = kuma_pass()
handled = {}
ewma = {}     # per-container klouzavý baseline chybovosti (Loki)
recur = {}    # key -> (count, first_ts): verify-and-escalate na úrovni detekce

RECUR_ESCALATE = 3        # 3× v okně → agent to trvale neopravil → eskaluj člověku
RECUR_WINDOW = 4 * 3600   # okno pro počítání opakování
RECUR_BACKOFF = 4 * 3600  # po eskalaci prodluž cooldown (přestaň mlátit to samé)


def notify(msg):
    try:
        subprocess.run([NOTIFY, msg], timeout=20)
    except Exception:
        pass


def log_incident(text):
    try:
        with open(INCIDENTS_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def alive_ping():
    try:
        url = open(ALIVE_PUSH_FILE).read().strip()
        if url:
            subprocess.run(["curl", "-fsS", "-m", "10", f"{url}?status=up&msg=OK"],
                           timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def trigger(key, incident):
    now = time.time()
    if now - handled.get(key, 0) < COOLDOWN:
        return
    handled[key] = now
    # Verify-and-escalate: když se incident vrací (agent tvrdil OPRAVENO, ale nedrží),
    # po RECUR_ESCALATE opakováních v okně eskaluj člověku a přestaň spouštět agenta.
    c, first = recur.get(key, (0, now))
    if now - first > RECUR_WINDOW:
        c, first = 0, now
    c += 1
    recur[key] = (c, first)
    if c >= RECUR_ESCALATE:
        handled[key] = now + RECUR_BACKOFF - COOLDOWN   # prodluž cooldown (back-off)
        msg = (f"opakovaný incident '{key}' ({c}× za {int((now - first) / 60)} min) — "
               f"self-healing to nedokázal trvale opravit. Nutný zásah člověka.")
        log_incident("ESCALATION (recurrence): " + msg)
        notify("🔁 " + msg)
        return
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TRIGGER agent: {key} (pokus {c})", flush=True)
    subprocess.Popen([RESPOND, incident])


def _prom_wget(url):
    return subprocess.check_output(
        ["sudo", "docker", "exec", PROM_CONTAINER, "wget", "-qO-", url],
        timeout=25, text=True)


# ── Zdroj 1: Uptime Kuma ─────────────────────────────────────────────────────
def _heartbeats_for(hb, mid):
    if isinstance(hb, dict):
        return hb.get(mid) or hb.get(str(mid)) or []
    if isinstance(hb, list):
        return [b for b in hb if isinstance(b, dict) and b.get("monitorID") == mid]
    return []


def poll_kuma():
    from uptime_kuma_api import UptimeKumaApi
    api = UptimeKumaApi(KUMA)
    try:
        api.login(USER, PASS)
        monitors = api.get_monitors()
        hb = api.get_heartbeats()
        for m in monitors:
            if not isinstance(m, dict):
                continue
            mid, name = m.get("id"), m.get("name", "?")
            beats = _heartbeats_for(hb, mid)
            if not beats:
                continue
            last = beats[-1]
            if isinstance(last, dict) and last.get("status") == 0:
                trigger(f"kuma:{name}", f"Monitor '{name}' je DOWN. Kuma msg: {last.get('msg','')}")
    finally:
        api.disconnect()


# ── Zdroj 2: Prometheus FIRING alerty ────────────────────────────────────────
def poll_prometheus():
    data = json.loads(_prom_wget("http://localhost:9090/api/v1/alerts"))
    for a in data.get("data", {}).get("alerts", []):
        if a.get("state") != "firing":
            continue
        lb = a.get("labels", {})
        name = lb.get("alertname", "?")
        if name not in ACTIONABLE:
            continue
        sev = lb.get("severity", "?")
        inst = lb.get("instance") or lb.get("container") or lb.get("name") or lb.get("job") or ""
        ann = a.get("annotations", {})
        summary = ann.get("summary") or ann.get("description") or ""
        incident = (f"Prometheus alert '{name}' (severity={sev}) FIRING. "
                    f"{('Cíl: ' + inst + '. ') if inst else ''}{summary}").strip()
        trigger(f"prom:{name}:{inst}", incident)


# ── Zdroj 3: Loki chybové spiky (spike-detekce přes EWMA) ────────────────────
def poll_loki():
    q = urllib.parse.quote(LOKI_QUERY)
    data = json.loads(_prom_wget(f"http://obs-loki:3100/loki/api/v1/query?query={q}"))
    for r in data.get("data", {}).get("result", []):
        cont = r.get("metric", {}).get("container", "?")
        try:
            val = float(r.get("value", [None, "0"])[1])
        except Exception:
            continue
        base = ewma.get(cont)
        if base is None:
            ewma[cont] = val            # warm-up: první pozorování jen nastaví baseline
            continue
        if val >= LOKI_SEVERE_MIN and val > LOKI_SPIKE_MULT * max(base, 1.0):
            trigger(
                f"loki:{cont}",
                f"Loki: kontejner '{cont}' má SPIKE závažných chyb v logách "
                f"({int(val)} shod za 5m, baseline ~{base:.0f}). Vzory: panic/fatal/OOM/"
                f"too-many-connections/5xx. Zdiagnostikuj z `docker logs` příčinu a "
                f"bezpečně oprav dle CLAUDE.md (při nejistotě ESKALACE).",
            )
        ewma[cont] = 0.7 * base + 0.3 * val   # posuň baseline (sustained stav přestane alertovat)


print("[self-healing poller] start (Kuma + Prometheus + Loki)", flush=True)
consec = {"kuma": 0, "prom": 0, "loki": 0}
escalated = {"kuma": False, "prom": False, "loki": False}


def run_source(tag, fn):
    try:
        fn()
        consec[tag] = 0
        escalated[tag] = False
    except Exception as e:
        consec[tag] += 1
        print(f"{tag} poll error ({consec[tag]}x): {e}", flush=True)
        if consec[tag] >= ERROR_ESCALATE_AFTER and not escalated[tag]:
            escalated[tag] = True
            msg = (f"self-healing poller: zdroj '{tag}' selhává {consec[tag]}x po sobě: {e}. "
                   "Detekce je slepá — zkontroluj.")
            log_incident("ESCALATION: " + msg)
            notify("⚠️ " + msg)


while True:
    run_source("kuma", poll_kuma)
    run_source("prom", poll_prometheus)
    run_source("loki", poll_loki)
    alive_ping()
    time.sleep(90)
