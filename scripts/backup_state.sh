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

    # R-B54 (13.08.2026, Audit-Finding — DRITTES Einfrieren dieser Liste):
    # seit R-B30 neu entstandene Dateien. Strukturelle Absicherung jetzt via
    # tests/test_backup_liste_vollstaendig.py (kritische Datei fehlt -> Suite rot).
    # (1) UNERSETZLICH:
    "signal_pit_snapshots.json"      # Point-in-Time-Kurs/Signal-Cache (1.8 MB)
                                     # der monatlichen Stack-Karte — laut R-B41
                                     # nicht look-ahead-frei rekonstruierbar.
    # (2) TEUER / lebendige Anzeige-Quellen:
    "stack_wfo_baseline.json"        # monatlich regenerierte Stack-Baseline —
                                     # der Namens-Zwilling wfo_signal_stack_
                                     # baseline.json (oben) ist der TOTE Stand.
    "signal_ic_report.json"          # woechentlicher IC-Report (So 08:00 CH).
    "signal_stack_shadow.json"       # aktuelle Kauf-Rangliste (Mo-Fr 23:30 CH).
    "health_audit_state.json"        # Sa-Audit-Befunde (R-A12-Historie).
    "alert_state.json"               # Herzschlag + last_daily_summary — auch
                                     # forensisch nuetzlich (Audit-Finding D-F5).
    # (3) MEILENSTEIN-ZUSTAND:
    "zwischencheck25_state.json"     # Einmal-Marker des 25er-Weckers (R-B53).
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

# R-B54: supervisor_state.json liegt bewusst auf HOST-Ebene (/opt/investpilot,
# ausserhalb von data/ — der Supervisor lebt ausserhalb des Containers) und
# fehlte deshalb im Backup. Separat anhaengen (tar -r geht nicht auf .gz,
# darum eigener kleiner Tarball daneben).
if [ -f /opt/investpilot/supervisor_state.json ]; then
    tar -czf "${BACKUP_DIR}/host_state_${TIMESTAMP}.tar.gz" \
        -C /opt/investpilot supervisor_state.json
    find "$BACKUP_DIR" -name "host_state_*.tar.gz" -mtime +${RETENTION_DAYS} -delete
fi
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
