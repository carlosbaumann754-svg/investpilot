#!/usr/bin/env bash
# investpilot — Cutover-Switch (Paper -> Live)
#
# R-A31 (21.05.2026): Atomarer 2-File-Switch fuer den Real-Money-Cutover am
# Mo 01.06.2026. Das alte Runbook erwaehnte nur den TRADING_MODE-Switch im
# ib-gateway-compose; der zweite Halbe (config.json port 4004 -> 4001) fehlte.
# Cutover ohne Step 2 -> Bot connectet weiter auf socat-bridge 4004 = nicht
# vorhanden im Live-Mode des gnzsnz/ib-gateway-Image -> Connect-Fail oder
# undefiniertes Verhalten am Cutover-Tag.
#
# Was dieses Skript macht:
#   1. Pre-Checks (Files existieren, aktueller Mode = paper)
#   2. Backup beider Files mit Timestamp (Rollback-Pfad)
#   3. Step 1: ib-gateway-compose TRADING_MODE paper -> live
#   4. Step 2: config.json ibkr.port 4004 -> 4001
#   5. Beide Container restart (ib-gateway zuerst, dann investpilot)
#   6. Post-Verify: /api/broker-status muss "mode":"live" + "account":"U..."
#      zeigen (nicht DU... = paper)
#
# Sicherheit:
#   - --dry-run zeigt Diff ohne Aenderung
#   - --rollback nimmt das letzte Backup-Pair zurueck (paper)
#   - Bei jeder Aenderung Verify; bei Fail automatischer Rollback
#   - Confirmation prompt vor Live-Switch (--force ueberspringt das)
#
# Verwendung:
#   bash scripts/cutover_switch.sh --dry-run        # zeigt was passiert
#   bash scripts/cutover_switch.sh                  # interaktiv mit Confirm
#   bash scripts/cutover_switch.sh --force          # ohne Confirm (CI)
#   bash scripts/cutover_switch.sh --rollback       # zurueck auf paper

set -euo pipefail

# ============================================================
# Konfiguration
# ============================================================

IBG_COMPOSE="/opt/ib-gateway/docker-compose.yml"
IBG_COMPOSE_NAME="docker-compose.yml"
IP_COMPOSE="/opt/investpilot/docker-compose.vps.yml"
IP_CONFIG="/opt/investpilot/data/config.json"

BACKUP_DIR="/var/backups/investpilot/cutover"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

PORT_PAPER=4004
PORT_LIVE=4001

LOG_PREFIX="[cutover_switch]"

# ============================================================
# Helpers
# ============================================================

log()  { echo "$LOG_PREFIX $*"; }
warn() { echo "$LOG_PREFIX WARN: $*" >&2; }
err()  { echo "$LOG_PREFIX ERROR: $*" >&2; }

confirm() {
    # $1 = prompt
    if [ "${FORCE:-0}" = "1" ]; then
        log "Auto-confirm (--force): $1"
        return 0
    fi
    read -r -p "$1 [yes/NO] " ans
    [ "$ans" = "yes" ]
}

require_root() {
    if [ "$EUID" -ne 0 ]; then
        err "Muss als root laufen (sudo bash $0)."
        exit 2
    fi
}

# ============================================================
# Pre-Checks
# ============================================================

pre_checks() {
    log "Pre-Checks..."
    for f in "$IBG_COMPOSE" "$IP_COMPOSE" "$IP_CONFIG"; do
        if [ ! -f "$f" ]; then
            err "Datei fehlt: $f"
            exit 3
        fi
    done

    if ! grep -qE "^\s*-\s*TRADING_MODE=paper\s*$" "$IBG_COMPOSE"; then
        warn "ib-gateway hat KEIN TRADING_MODE=paper (vielleicht schon live, oder Format anders?)"
        grep -nE "TRADING_MODE" "$IBG_COMPOSE" || true
        confirm "Trotzdem weitermachen?" || exit 4
    fi

    local current_port
    current_port=$(python3 -c "import json; print(json.load(open('$IP_CONFIG'))['ibkr']['port'])")
    if [ "$current_port" != "$PORT_PAPER" ]; then
        warn "config.json ibkr.port ist nicht $PORT_PAPER (sondern $current_port)"
        confirm "Trotzdem weitermachen?" || exit 5
    fi

    log "Pre-Checks ok: aktuelles Setup = PAPER."
}

# ============================================================
# Backup
# ============================================================

do_backup() {
    log "Backup nach $BACKUP_DIR/${TIMESTAMP}_*"
    mkdir -p "$BACKUP_DIR"
    cp -p "$IBG_COMPOSE" "$BACKUP_DIR/${TIMESTAMP}_ibgateway_${IBG_COMPOSE_NAME}.bak"
    cp -p "$IP_CONFIG"   "$BACKUP_DIR/${TIMESTAMP}_investpilot_config.json.bak"
    echo "$TIMESTAMP" > "$BACKUP_DIR/LATEST"
    log "Backup: $BACKUP_DIR/${TIMESTAMP}_*"
}

# ============================================================
# Switch-Logik
# ============================================================

show_diff() {
    log "Step 1 — ib-gateway TRADING_MODE:"
    diff <(grep -E "TRADING_MODE" "$IBG_COMPOSE") \
         <(sed 's/TRADING_MODE=paper/TRADING_MODE=live/' "$IBG_COMPOSE" | grep -E "TRADING_MODE") \
         || true

    log "Step 2 — investpilot ibkr.port:"
    python3 -c "
import json
cfg = json.load(open('$IP_CONFIG'))
print(f'  current: {cfg[\"ibkr\"][\"port\"]} (paper)')
print(f'  target:  $PORT_LIVE (live)')
"
}

apply_switch() {
    log "Apply Step 1: $IBG_COMPOSE TRADING_MODE paper -> live"
    sed -i.tmp 's/TRADING_MODE=paper/TRADING_MODE=live/' "$IBG_COMPOSE"
    rm -f "${IBG_COMPOSE}.tmp"

    log "Apply Step 2: $IP_CONFIG ibkr.port $PORT_PAPER -> $PORT_LIVE"
    python3 -c "
import json
with open('$IP_CONFIG') as f:
    cfg = json.load(f)
cfg['ibkr']['port'] = $PORT_LIVE
if isinstance(cfg.get('ibkr', {}).get('_comment'), str):
    cfg['ibkr']['_comment'] = (
        cfg['ibkr']['_comment']
        + ' | R-A31 ($TIMESTAMP): port -> $PORT_LIVE (LIVE socat-bridge).'
    )
with open('$IP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
"
    log "Beide Files updated."
}

restart_containers() {
    log "Restart ib-gateway..."
    docker compose -f "$IBG_COMPOSE" up -d --force-recreate ib-gateway
    log "Warte 30s bis ib-gateway hochgefahren ist..."
    sleep 30

    log "Restart investpilot..."
    docker compose -f "$IP_COMPOSE" up -d --force-recreate investpilot
    log "Warte 20s bis investpilot connected ist..."
    sleep 20
}

verify_live() {
    # R-B11/C1 (07.06.2026): /api/broker-status emittiert fuer IBKR mode=real
    # (NICHT der alte live-String, den es im Code gar nicht gibt) + account_type
    # live. Frueher wurde auf den live-Mode-String geprueft -> verify schlug IMMER
    # fehl -> Auto-Rollback -> der Cutover konnte strukturell nie durchgehen.
    # account_type ist non-sensitive (R-B11/C3): die rohe Account-Nr wird im
    # unauth-Response nicht mehr geleakt, daher hier darueber verifizieren.
    log "Verify: /api/broker-status sollte mode=real + account_type=live + connected=true zeigen"
    local resp
    resp=$(curl -s -m 10 http://localhost:8000/api/broker-status || echo '{}')
    log "Response: $resp"

    if echo "$resp" | grep -q '"mode":"real"' && \
       echo "$resp" | grep -q '"account_type":"live"' && \
       echo "$resp" | grep -q '"connected":true'; then
        log "VERIFY OK — Cutover erfolgreich."
        return 0
    fi
    err "VERIFY FAIL — Response zeigt nicht real+live-account+connected."
    return 1
}

# ============================================================
# Rollback
# ============================================================

rollback() {
    require_root
    if [ ! -f "$BACKUP_DIR/LATEST" ]; then
        err "Kein Backup-Pair (LATEST-Marker fehlt)."
        exit 6
    fi
    local ts
    ts=$(cat "$BACKUP_DIR/LATEST")
    log "Rollback auf Backup-Pair $ts..."

    cp -p "$BACKUP_DIR/${ts}_ibgateway_${IBG_COMPOSE_NAME}.bak" "$IBG_COMPOSE"
    cp -p "$BACKUP_DIR/${ts}_investpilot_config.json.bak"      "$IP_CONFIG"
    log "Files zurueck (paper)."

    restart_containers

    log "Verify rollback..."
    local resp
    resp=$(curl -s -m 10 http://localhost:8000/api/broker-status || echo '{}')
    log "Response: $resp"
    if echo "$resp" | grep -q '"mode":"paper"'; then
        log "ROLLBACK OK — wieder auf paper."
        exit 0
    fi
    err "ROLLBACK PROBLEM — manuell checken."
    exit 7
}

# ============================================================
# Main
# ============================================================

main() {
    local dry_run=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry_run=1 ;;
            --force)   export FORCE=1 ;;
            --rollback) rollback ;;
            -h|--help)
                grep -E '^# ' "$0" | head -50
                exit 0 ;;
            *) err "Unbekanntes Argument: $1"; exit 1 ;;
        esac
        shift
    done

    require_root
    pre_checks
    show_diff

    if [ "$dry_run" = "1" ]; then
        log "DRY-RUN — keine Aenderungen vorgenommen. Exit."
        exit 0
    fi

    confirm "Switch PAPER -> LIVE jetzt durchfuehren? (Backup wird automatisch erstellt)" || {
        log "Abgebrochen."
        exit 0
    }

    do_backup
    apply_switch
    restart_containers

    if ! verify_live; then
        err "VERIFY fehlgeschlagen — automatischer Rollback."
        rollback
    fi

    log "DONE — Live-Cutover erfolgreich."
}

main "$@"
