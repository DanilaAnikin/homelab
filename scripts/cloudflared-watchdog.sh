#!/usr/bin/env bash
# =============================================================================
# cloudflared-watchdog.sh — restartuje tunel, když uvízne v neúspěšném dialu.
#
# Proč vůbec existuje: 9. 9. 2026 odešel doma ethernet. Linka se vrátila
# ve 12:44:51, ale cloudflared se sám nezotavil — dalších 2 h 18 min sypal
# "failed to dial to edge with quic" a spojení navázal až v 15:03:02.
# systemd s tím nemohl nic udělat: proces nikdy neskončil, takže se
# Restart=on-failure nespustil. Veřejný výpadek byl 4 h 39 min místo 2 h 20.
#
# Zásah je schválně podmíněný třemi věcmi naráz, aby to nerestartovalo naslepo:
#   1) cloudflared běží (když neběží, řeší to systemd sám),
#   2) internet z tohohle stroje FUNGUJE (jinak není co spravovat a restart
#      by se během skutečného výpadku opakoval donekonečna),
#   3) v posledních minutách jsou chyby dialu a ZÁROVEŇ žádné úspěšné
#      registrované spojení.
#
# Navíc drží prodlevu mezi restarty a denní strop. Když strop dojde, radši
# přestane restartovat a zavolá člověka — opakovaný restart, který nepomáhá,
# jen zakrývá hlubší příčinu.
# =============================================================================
set -uo pipefail

OKNO_MIN=3            # jak daleko do minulosti hledat důkazy
PRODLEVA_S=900        # nejmenší odstup mezi dvěma restarty (15 min)
DENNI_STROP=6         # víc restartů za den = problém je jinde, volej člověka
STAV_DIR=/var/lib/homelab
STAV=$STAV_DIR/cloudflared-watchdog
source "${HOMELAB_ALERT_LIB:-/usr/local/libexec/homelab-alert.sh}" || exit 1

mkdir -p "$STAV_DIR"
log(){ echo "cloudflared-watchdog: $*"; }
alert(){ homelab_alert "cloudflared:connectivity" "$1" "$2" "${3:-false}" 1800 || true; }

# --- 1) běží vůbec? ----------------------------------------------------------
if ! systemctl is-active --quiet cloudflared; then
  log "cloudflared neběží — nechávám na systemd (Restart=on-failure)"
  exit 0
fi

# --- 2) máme z tohohle stroje internet? --------------------------------------
# Bez tohohle by watchdog během skutečného výpadku linky restartoval tunel
# každé dvě minuty a jen zahlcoval log. Když net nejede, není co spravovat.
if ! curl -sf --max-time 8 -o /dev/null https://api.cloudflare.com/client/v4/ips; then
  log "internet z tohoto stroje nejede — nezasahuji"
  exit 0
fi

# --- 3) vypadá tunel zaseknutě? ----------------------------------------------
OKNO="-${OKNO_MIN}min"
CHYBY=$(journalctl -u cloudflared --since "$OKNO" --no-pager 2>/dev/null \
        | grep -cE "Failed to dial a quic connection|no recent network activity|failed to dial to edge")
USPECH=$(journalctl -u cloudflared --since "$OKNO" --no-pager 2>/dev/null \
        | grep -c "Registered tunnel connection")

if (( CHYBY == 0 )); then
  alert resolved "Tunel nemá chyby připojení."
  exit 0                       # žádné chyby = tunel je v pořádku, mlčíme
fi
if (( USPECH > 0 )); then
  log "chyby ($CHYBY) i úspěšné registrace ($USPECH) — tunel se zotavuje sám"
  alert resolved "Tunel se připojil."
  exit 0
fi

# --- 4) prodleva a denní strop ------------------------------------------------
TED=$(date +%s)
DNES=$(date +%F)
POSLEDNI=0; POCET=0; DEN=""
[[ -f $STAV ]] && read -r POSLEDNI POCET DEN < "$STAV" 2>/dev/null || true
[[ "$DEN" != "$DNES" ]] && { POCET=0; DEN=$DNES; }

if (( TED - POSLEDNI < PRODLEVA_S )); then
  log "restart byl před $((TED - POSLEDNI)) s, čekám na prodlevu ${PRODLEVA_S}s"
  alert firing "Cloudflared se ani po automatickém restartu nepřipojuje. Zkontroluj journalctl -u cloudflared."
  exit 0
fi

if (( POCET >= DENNI_STROP )); then
  log "denní strop $DENNI_STROP restartů vyčerpán — nezasahuji, volám člověka"
  alert firing "Cloudflared se nepřipojuje ani po $POCET automatických restartech. Další restarty jsou zastavené; zkontroluj journalctl -u cloudflared." true
  exit 0
fi

# --- 5) zásah -----------------------------------------------------------------
log "tunel zaseknutý ($CHYBY chyb dialu, 0 registrací za $OKNO_MIN min) — restartuji"
if systemctl restart cloudflared; then
  POCET=$((POCET + 1))
  printf '%s %s %s\n' "$TED" "$POCET" "$DNES" > "$STAV"
  sleep 20
  NOVE=$(journalctl -u cloudflared --since "-30s" --no-pager 2>/dev/null | grep -c "Registered tunnel connection")
  if (( NOVE > 0 )); then
    log "po restartu navázáno $NOVE spojení"
    alert resolved "Cloudflared se po restartu připojil."
  else
    log "po restartu zatím žádné spojení"
    alert firing "Cloudflared se ani po automatickém restartu nepřipojuje. Zkontroluj journalctl -u cloudflared."
  fi
else
  log "restart cloudflared SELHAL"
  alert firing "Automatický restart cloudflared selhal. Zkontroluj systemctl status cloudflared." true
  exit 1
fi
