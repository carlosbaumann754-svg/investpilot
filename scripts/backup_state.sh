#!/bin/bash
# v37n: Daily Backup of critical bot state files (Hard-Gate #4)
# ==============================================================
# Wird via VPS-Cron ausgefuehrt (04:00 UTC = vor US-Pre-Market).
# Erstellt taeglich einen tarball mit Risk-State, Brain-State, Config,
# Trade-History, Cost-Model-Calibration, Insider-Shadow-Log.
# Retention: 30 Tage (rolling), aelter wird automatisch geloescht.
#
# Plus zusaetzliche Cloud-Sicherung via Bot-internem Gist-Backup (laeuft
# bereits jeden Cycle automatisch). Diese Backups hier sind die LOCAL-
# ON-DISK-Kopie, falls Bot-Container/IBKR-Daten korrupt werden.

set -euo pipefail

BACKUP_DIR="/var/backups/investpilot"
SOURCE_DIR="/opt/investpilot/data"
RETENTION_DAYS=30
TIMESTAMP=$(date -u +"%Y-%m-%d_%H%M%S")
ARCHIVE="${BACKUP_DIR}/state_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

# Liste der kritischen Dateien (relativ zu data/)
FILES=(
    "config.json"
    "risk_state.json"
    "brain_state.json"
    "trade_history.json"
    "cost_model_calibration.json"
    "insider_shadow_log.jsonl"
    "wfo_history.json"
    "wfo_status.json"
    "survivorship_history.json"
    "auth_2fa.json"
    "ibkr_contract_cache.json"

    # R-B30 (21.07.2026): Die Liste war seit April eingefroren, waehrend das
    # data/-Verzeichnis wuchs. Carlos' Frage "wo werden die Daten gespeichert"
    # deckte auf: zehn wichtige Dateien lagen NUR auf der VPS-Platte.
    # Nach Schadensschwere ergaenzt:

    # (1) UNERSETZLICH — laesst sich nicht rekonstruieren:
    "signal_score_history.json"      # taegl. Point-in-Time-Scores (R-B25/R-B30).
                                     # Rekonstruktion = Look-Ahead-Bias, der
                                     # Verlust von 6 Wochen war der Ausloeser
                                     # des ganzen Moduls.
    "equity_history.json"            # taegl. Equity-Snapshots inkl. USD/CHF —
                                     # Kursverlauf des eigenen Depots.

    # (2) TEUER — rekonstruierbar, aber mit Stunden Aufwand/Risiko:
    "manual_lock_overrides.json"     # validierte Post-Soak-Werte; Verlust
                                     # wuerde Alt-Motor-WFO-Locks reaktivieren
                                     # (Bot friert bei 11 Positionen ein).
    "roundtrip_pf_reference.json"    # Referenzverteilung; ohne sie ist der
                                     # Motor-Edge-Alarm blind (Staleness-Guard
                                     # blockt dann JEDEN Alarm).
    "cutover_confirmations.json"     # Audit-Trail der Hard-Gate-Bestaetigungen
                                     # (z.B. Master-2FA) — Nachweis, nicht Zahl.
    "wfo_signal_stack_baseline.json" # WFO-Baseline des neuen Motors.

    # (3) LAUFZEIT-ZUSTAND — klein, aber Verlust aendert Bot-VERHALTEN:
    "trailing_sl_state.json"         # Ratchet-Staende; Verlust = Trailing faellt
                                     # auf Entry-Basis zurueck -> Stops weiter
                                     # als der Hoechststand rechtfertigt.
    "partial_close_state.json"       # welche Tranchen je Position schon feuerten
                                     # (Tranchen sind aus, Historie bleibt).
    "buy_cooldown.json"              # verhindert Sofort-Rueckkauf nach Exit.
)

# Sammle nur Dateien die existieren (some erst spaeter angelegt)
EXISTING_FILES=()
for f in "${FILES[@]}"; do
    if [ -f "${SOURCE_DIR}/${f}" ]; then
        EXISTING_FILES+=("$f")
    fi
done

if [ ${#EXISTING_FILES[@]} -eq 0 ]; then
    echo "[$(date -u +%FT%TZ)] FEHLER: keine Backup-Dateien gefunden in ${SOURCE_DIR}" >&2
    exit 1
fi

# tar.gz mit relativen Pfaden
tar -czf "$ARCHIVE" -C "$SOURCE_DIR" "${EXISTING_FILES[@]}"
SIZE=$(stat -c%s "$ARCHIVE")
echo "[$(date -u +%FT%TZ)] Backup OK: ${ARCHIVE} (${SIZE} bytes, ${#EXISTING_FILES[@]} Dateien)"

# Retention: loeschen was aelter als RETENTION_DAYS
DELETED=$(find "$BACKUP_DIR" -name "state_*.tar.gz" -mtime +${RETENTION_DAYS} -print -delete | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "[$(date -u +%FT%TZ)] Retention: ${DELETED} alte Backups (>${RETENTION_DAYS}d) geloescht"
fi

# Letzte Backup-Info fuer /api/backups/status
INFO_FILE="${BACKUP_DIR}/last_backup.json"
cat > "$INFO_FILE" << EOF
{
    "last_backup_at": "$(date -u +%FT%TZ)",
    "archive": "${ARCHIVE}",
    "size_bytes": ${SIZE},
    "files_included": ${#EXISTING_FILES[@]},
    "retention_days": ${RETENTION_DAYS}
}
EOF
