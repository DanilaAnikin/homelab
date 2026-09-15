#!/usr/bin/env bash
# ============================================================================
# provision-supabase-stack.sh <app> <api_domain>
# Postaví per-app self-hosted Supabase stack na homelabu (Freio pattern,
# generalizovaný). Namespacing: compose projekt -p <app>-supabase + strip
# container_name; jen kong dostane stabilní jméno <app>-supabase-kong na
# dokploy-network (Traefik ho tak najde: Host(api_domain) -> kong:8000).
#
# Idempotentní-ish: přepíše compose/env, znovu up -d.
# NEspouští migrace (dělá volající zvlášť po ověření, že stack běží).
# ============================================================================
set -euo pipefail
APP="${1:?app name, napr. gorillatype}"
APIDOM="${2:?api domain, napr. gtapi.anikin.cz}"
BASE="/srv/homelab/compose/supabase-$APP"
UPSTREAM_SRC="/srv/homelab/freio-rehearsal/supabase-docker"   # už fetchnutý upstream
PROJECT="$APP-supabase"
KONG_NAME="$APP-supabase-kong"
NET_INTERNAL="${PROJECT}_default"

echo "== provision Supabase stack: $APP ($APIDOM) =="
mkdir -p "$BASE"

# ── 1) upstream base (kopie fetchnutého) ────────────────────────────────────
if [[ ! -f "$BASE/docker-compose.yml" ]]; then
  cp "$UPSTREAM_SRC/docker-compose.yml" "$BASE/docker-compose.base.yml"
  [[ -d "$UPSTREAM_SRC/volumes" ]] && cp -rn "$UPSTREAM_SRC/volumes" "$BASE/volumes" 2>/dev/null || true
fi

# ── 2) patch base: namespacing + hardening (Python, robustní) ───────────────
python3 - "$BASE" "$APP" "$KONG_NAME" <<'PY'
import sys, yaml
base, app, kong_name = sys.argv[1], sys.argv[2], sys.argv[3]
f = f"{base}/docker-compose.base.yml"
d = yaml.safe_load(open(f))
svcs = d.get("services", {})

# Odeber služby, které nechceme (pooler + host porty; analytics/vector obvykle nejsou).
for drop in ("supavisor", "analytics", "vector"):
    svcs.pop(drop, None)

# Odeber depends_on odkazy na smazané + na analytics/vector; zbav host portů.
for name, svc in list(svcs.items()):
    dep = svc.get("depends_on")
    if isinstance(dep, dict):
        for k in list(dep):
            if k in ("supavisor", "analytics", "vector"):
                dep.pop(k, None)
    elif isinstance(dep, list):
        svc["depends_on"] = [x for x in dep if x not in ("supavisor","analytics","vector")]
    # žádné host porty (tunnel/traefik řeší přístup); ponech jen interní expose
    svc.pop("ports", None)
    # strip container_name (compose -p pak auto-pojmenuje, žádná kolize s freio)
    svc.pop("container_name", None)

# kong: stabilní jméno + na dokploy-network (Traefik ho najde)
if "kong" in svcs:
    svcs["kong"]["container_name"] = kong_name
    nets = svcs["kong"].get("networks")
    if nets is None:
        svcs["kong"]["networks"] = ["default", "dokploy-network"]
    elif isinstance(nets, list):
        if "dokploy-network" not in nets: nets.append("dokploy-network")
        if "default" not in nets: nets.append("default")
    elif isinstance(nets, dict):
        nets["dokploy-network"] = {}

# realtime: alias "realtime-dev.supabase-realtime" na VLASTNÍ síti stacku.
# Upstream kong.yml míří na tohle jméno a není to jen DNS — Realtime z prvního
# labelu hostname odvozuje tenanta, a seedovaný tenant se jmenuje "realtime-dev".
# Výš všem službám mažeme container_name (kvůli kolizím mezi stacky), takže bez
# aliasu Kong jméno nepřeloží a živé aktualizace přes WebSocket jsou mrtvé
# (klient dostane 1006). Přepsat upstream na "realtime" nejde: DNS pak projde,
# ale Realtime odpoví TenantNotFound. Alias na "default" síti nekoliduje s jinými
# stacky na dokploy-network. Zjištěno 15. 9. 2026 na dashboardu farmy.
if "realtime" in svcs:
    rt = svcs["realtime"]
    alias = "realtime-dev.supabase-realtime"
    nets = rt.get("networks")
    if nets is None:
        rt["networks"] = {"default": {"aliases": [alias]}}
    elif isinstance(nets, list):
        rt["networks"] = {n: {} for n in nets}
        rt["networks"].setdefault("default", {})
        rt["networks"]["default"]["aliases"] = [alias]
    elif isinstance(nets, dict):
        dflt = nets.get("default") or {}
        al = list(dflt.get("aliases") or [])
        if alias not in al:
            al.append(alias)
        dflt["aliases"] = al
        nets["default"] = dflt

# přidej externí dokploy-network do top-level networks
d.setdefault("networks", {})
d["networks"]["dokploy-network"] = {"external": True}

yaml.safe_dump(d, open(f"{base}/docker-compose.yml","w"), sort_keys=False, default_flow_style=False)
print("  patched:", list(svcs.keys()))
PY

echo "  base compose: $BASE/docker-compose.yml"
echo "PROJECT=$PROJECT KONG=$KONG_NAME NET=$NET_INTERNAL"
