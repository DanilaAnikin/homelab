#!/usr/bin/env python3
"""Self-healing poller: dva zdroje incidentů → respond.sh (dedup + cooldown).

  1) Uptime Kuma DOWN monitory (web/DB/infra dostupnost).
  2) Prometheus FIRING alerty (disk/crashloop/cert/pamět/postgres) — actionable
     třídy, které agent umí bezpečně opravit dle CLAUDE.md. Prometheus/Alertmanager
     nejsou publikované na host (Swarm overlay), proto se čtou přes `docker exec`.

Robustní: každý zdroj má vlastní try/except (jeden padlý neoslepí druhý) a po N
po sobě jdoucích chybách pollu eskaluje na Telegram + do incidents.log.
"""
import subprocess, time, json

KUMA = "http://localhost:3001"; USER = "DanilaAnikin"
COOLDOWN = 1800           # 30 min per incident-key (ať agent nemlátí to samé dokola)
ERROR_ESCALATE_AFTER = 5  # po tolika po sobě jdoucích chybách pollu eskaluj (~7.5 min)
INCIDENTS_LOG = "/srv/homelab/self-healing/incidents.log"
NOTIFY = "/srv/homelab/self-healing/notify.sh"
RESPOND = "/srv/homelab/self-healing/respond.sh"
PROM_CONTAINER = "obs-prometheus"
# Watchdog-on-watchdog: poller pushuje heartbeat do Kuma "self-healing alive" monitoru.
# Když poller tiše zamrzne/umře, monitor jde DOWN a Kuma SÁM (nezávisle) pošle Telegram —
# poller se totiž neumí hlídat sám. Soubor obsahuje jen push URL (nebo je prázdný).
ALIVE_PUSH_FILE = "/srv/homelab/secrets/kuma-selfheal-push-url.txt"

# Prometheus alerty, které agent umí BEZPEČNĚ řešit dle CLAUDE.md runbooku.
# (WebNedostupny řeší Kuma; info/predictive/frem-specific se zde záměrně nespouští.)
ACTIONABLE = {
    "DiskDochazi", "KontejnerSeRestartuje", "PostgresNedostupny",
    "MaloPameti", "KontejnerZeraPamet", "PostgresDochazejiSpojeni",
    "CertifikatBrzyVyprsi",
}


def kuma_pass():
    for line in open("/srv/homelab/secrets/kuma-login.txt"):
        if line.startswith("KUMA_PASS="):
            return line.split("=", 1)[1].strip()


PASS = kuma_pass()
handled = {}


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
    """Heartbeat do Kuma 'self-healing alive' — dokládá, že poller žije (ne zamrzl)."""
    try:
        url = open(ALIVE_PUSH_FILE).read().strip()
        if url:
            subprocess.run(["curl", "-fsS", "-m", "10", f"{url}?status=up&msg=OK"],
                           timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def trigger(key, incident):
    """Dedup dle key + cooldown → spusť responder."""
    now = time.time()
    if now - handled.get(key, 0) < COOLDOWN:
        return
    handled[key] = now
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TRIGGER agent: {key}", flush=True)
    subprocess.Popen([RESPOND, incident])


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


# ── Zdroj 2: Prometheus FIRING alerty (přes docker exec) ─────────────────────
def poll_prometheus():
    raw = subprocess.check_output(
        ["sudo", "docker", "exec", PROM_CONTAINER, "wget", "-qO-",
         "http://localhost:9090/api/v1/alerts"],
        timeout=25, text=True,
    )
    data = json.loads(raw)
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


print("[self-healing poller] start (Kuma + Prometheus)", flush=True)
consec = {"kuma": 0, "prom": 0}
escalated = {"kuma": False, "prom": False}


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
    alive_ping()   # heartbeat — poller žije (watchdog-on-watchdog)
    time.sleep(90)
