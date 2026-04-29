#!/bin/bash
# v37n: Backup-Restore Test (Hard-Gate #4 Verifikation)
# ======================================================
# Verifiziert dass das letzte Backup INTAKT und ENTPACKBAR ist.
# Schreibt die Dateien NICHT zurueck — entpackt nur in /tmp und prueft
# JSON-Validitaet + listet enthaltene Dateien.
#
# Manuell ausfuehren mit:  bash scripts/restore_state_test.sh
# Im Cron 1x pro Woche fuer Auto-Verifikation.

set -euo pipefail

BACKUP_DIR="/var/backups/investpilot"
TEST_DIR="/tmp/investpilot-restore-test-$$"

# Letztes Backup finden
LATEST=$(ls -t "${BACKUP_DIR}"/state_*.tar.gz 2>/dev/null | head -1 || true)
if [ -z "$LATEST" ]; then
    echo "[$(date -u +%FT%TZ)] FEHLER: kein Backup gefunden in ${BACKUP_DIR}" >&2
    exit 1
fi

echo "[$(date -u +%FT%TZ)] Teste Restore von: ${LATEST}"

mkdir -p "$TEST_DIR"
trap "rm -rf $TEST_DIR" EXIT

# Entpacken
if ! tar -xzf "$LATEST" -C "$TEST_DIR"; then
    echo "[$(date -u +%FT%TZ)] FEHLER: tar konnte Archive nicht entpacken" >&2
    exit 1
fi

# JSON-Validitaet pruefen
INVALID_COUNT=0
for f in "$TEST_DIR"/*.json; do
    if [ -f "$f" ]; then
        if ! python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
            echo "  INVALID JSON: $(basename $f)" >&2
            INVALID_COUNT=$((INVALID_COUNT + 1))
        fi
    fi
done

# JSONL pruefen (zeilenweise)
for f in "$TEST_DIR"/*.jsonl; do
    if [ -f "$f" ]; then
        if ! python3 -c "
import json, sys
with open('$f') as fp:
    for i, line in enumerate(fp, 1):
        if line.strip():
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                sys.exit(f'Line {i}: {e}')
" 2>/dev/null; then
            echo "  INVALID JSONL: $(basename $f)" >&2
            INVALID_COUNT=$((INVALID_COUNT + 1))
        fi
    fi
done

FILE_COUNT=$(find "$TEST_DIR" -type f | wc -l)
echo "[$(date -u +%FT%TZ)] Restore-Test: ${FILE_COUNT} Dateien entpackt, ${INVALID_COUNT} ungueltig"

if [ "$INVALID_COUNT" -gt 0 ]; then
    exit 2
fi

echo "[$(date -u +%FT%TZ)] OK — Backup ist sauber + restorebar"
