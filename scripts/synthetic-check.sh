#!/usr/bin/env bash
# ============================================================================
# synthetic-check.sh — hlubší funkční checky (ne jen HTTP 200): reálné flows.
# auth stack s apikey (GoTrue+kong+DB žijí), DB dotaz přes PostgREST, render webů.
# Retry-once proti transientním blipům; při skutečném selhání → Telegram.
# ============================================================================
set -uo pipefail
NOTIFY=/srv/homelab/self-healing/notify.sh
LOG=/srv/homelab/self-healing/synthetic-check.log
exec > >(tee -a "$LOG") 2>&1     # logovat vše BEZ subshellu (FAILS zůstane v main shellu)
FAILS=""
TMO=20

anon(){ grep -h "^ANON_KEY=" "$1" 2>/dev/null | cut -d= -f2; }
GT_ANON=$(anon /srv/homelab/compose/supabase-gorillatype/.env)
CL_ANON=$(anon /srv/homelab/compose/supabase-classio/.env)

# retry wrapper: N pokusů s odstupem 4 s (proti transientním blipům / SSR pomalosti)
retry(){ local n="$1"; shift; local i; for i in $(seq 1 "$n"); do "$@" && return 0; [[ "$i" -lt "$n" ]] && sleep 4; done; return 1; }

_http(){ curl -s -m "$TMO" -o /dev/null -w '%{http_code}' ${2:+-H "$2"} "$1"; }
check_http(){ # name url exp [header]
  local name="$1" url="$2" exp="$3" hdr="${4:-}" code
  code=$(_http "$url" "$hdr"); [[ "$code" == "$exp" ]] || { sleep 3; code=$(_http "$url" "$hdr"); }
  if [[ "$code" == "$exp" ]]; then echo "  ✓ $name ($code)"; else echo "  ✗ $name ($code≠$exp)"; FAILS+="$name(HTTP $code) "; fi
}
_rest(){ curl -s -m "$TMO" -o /dev/null -w '%{http_code}' -H "apikey: $2" -H "Authorization: Bearer $2" "$1"; }
check_rest(){ local name="$1" url="$2" key="$3" code; code=$(_rest "$url" "$key"); [[ "$code" == 200 ]] || { sleep 3; code=$(_rest "$url" "$key"); }
  if [[ "$code" == 200 ]]; then echo "  ✓ $name ($code)"; else echo "  ✗ $name ($code)"; FAILS+="$name(REST $code) "; fi
}
# POZOR: `curl | grep -q` + pipefail = false negativ (grep -q ukončí rouru brzy →
# curl dostane SIGPIPE → pipeline „selže" i při nálezu). Proto načteme do proměnné.
_kw(){ local b; b=$(curl -s -m 25 "$1" 2>/dev/null) || true; grep -qi "$2" <<<"$b"; }
check_keyword(){ local name="$1" url="$2" kw="$3"
  if retry 3 _kw "$url" "$kw"; then echo "  ✓ $name ('$kw')"; else echo "  ✗ $name (chybí '$kw' 3×)"; FAILS+="$name(render) "; fi
}

echo "═══ SYNTHETIC $(date -Iseconds) ═══"
[[ -n "$GT_ANON" ]] && check_http "gorilla auth" "https://gtapi.anikin.cz/auth/v1/health" 200 "apikey: $GT_ANON"
[[ -n "$CL_ANON" ]] && check_http "classio auth" "https://capi.anikin.cz/auth/v1/health" 200 "apikey: $CL_ANON"
[[ -n "$GT_ANON" ]] && check_rest "gorilla DB (profiles)" "https://gtapi.anikin.cz/rest/v1/profiles?limit=1" "$GT_ANON"
check_keyword "anikin.cz render" "https://anikin.cz" "Anikin"
check_keyword "gorillatype render" "https://gorillatype.anikin.cz" "gorilla"
check_keyword "classio render" "https://classio.anikin.cz" "Classio"
check_keyword "freio render" "https://freio.cz" "freio"
check_keyword "ripieno render" "https://www.ripieno.xyz" "Ripieno"

if [[ -n "$FAILS" ]]; then
  "$NOTIFY" "🧪 Synthetic check SELHAL: $FAILS — funkční problém (ne jen uptime)." || true
  exit 1
fi
echo "  ✓ vše OK"
exit 0
