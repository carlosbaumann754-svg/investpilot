#!/bin/bash
# scripts/semgrep_weekly.sh — Wochentlicher Security-Scan (v37f, 2026-04-29)
#
# Wird vom VPS-Cron Sonntag 14:00 UTC ausgefuehrt (1h nach Survivorship-Audit).
# Laeuft semgrep im offiziellen Docker-Image (nutzt p/python + p/secrets +
# p/owasp-top-ten Rules), schreibt JSON-Output in data/semgrep_latest.json.
# Anschliessend ruft den Bot-Container app.semgrep_runner auf, der den
# Diff zum vorigen Run berechnet und ggf. Telegram-Alert sendet.

set -e

SRC_DIR="/opt/investpilot"
LOG_TAG="[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] semgrep_weekly"

cd "$SRC_DIR"

echo "$LOG_TAG starting"

# 1. Semgrep-Scan via Docker (semgrep selbst hat kein Bot-Container-Setup,
#    waere Bloat). Read-only-Mount des Source-Codes, write nur ins data/-
#    Verzeichnis fuer den JSON-Output.
docker run --rm \
    -v "$SRC_DIR/app:/src/app:ro" \
    -v "$SRC_DIR/web:/src/web:ro" \
    -v "$SRC_DIR/scripts:/src/scripts:ro" \
    -v "$SRC_DIR/data:/src/data" \
    -w /src \
    returntocorp/semgrep:latest \
    semgrep scan \
        --config=p/python \
        --config=p/secrets \
        --config=p/owasp-top-ten \
        --severity ERROR \
        --severity WARNING \
        --json --output /src/data/semgrep_latest.json \
        --metrics=on \
        --quiet \
        app/ web/ scripts/ \
    2>&1 | tail -3

SCAN_EXIT=$?
echo "$LOG_TAG semgrep scan exit=$SCAN_EXIT"

# 2. Postprocessor im Bot-Container — liest JSON, vergleicht mit history,
#    triggert Telegram-Alert bei Anomalien.
docker exec investpilot python -m app.semgrep_runner cron-weekly 2>&1 | tail -5

PROCESS_EXIT=$?
echo "$LOG_TAG postprocess exit=$PROCESS_EXIT"
echo "$LOG_TAG complete"
