#!/bin/bash
# safe_env_update.sh — append-only .env-Update mit Auto-Backup
#
# WIESO (Lehre vom 08.05.2026):
# ============================
# Beim Sentry-Deploy (v37g) hat ein `cat > /opt/investpilot/.env <<EOF`-Befehl
# die ganze .env-File ueberschrieben — alle vorhandenen Variables (DASHBOARD_USERNAME,
# DASHBOARD_PASSWORD, JWT_SECRET, GITHUB_TOKEN, SMTP_*, ALERT_RECIPIENT) waren weg.
# Bot lief weiter (vorhandener Container hatte env in Memory), aber:
# - Login broken nach naechstem Restart
# - Cloud-Backup zur GitHub-Gist deaktiviert
# - Email-Alerts deaktiviert
# - JWT-Secret zufaellig generiert (= alle Sessions nach Restart invalidiert)
#
# Dieses Script verhindert Wiederholung durch:
#   1. Auto-Backup mit Timestamp VOR jeder Modifikation
#   2. Append-Only: existierende KEYS werden in-place ersetzt (sed), nie gedroppt
#   3. Verify: nach Update wird Key-Count gegen Pre-State geprueft. Bei Verlust
#      → Auto-Restore vom Backup + Exit-Fail
#   4. chmod 600 nach jedem Update (Secrets-Protection)
#
# USAGE:
# ======
#   # Single var
#   ./safe_env_update.sh "GITHUB_TOKEN=ghp_xxx"
#
#   # Multiple vars (newline-separated)
#   ./safe_env_update.sh "DASHBOARD_USERNAME=carlos
# DASHBOARD_PASSWORD=secret123"
#
#   # Batch from file
#   ./safe_env_update.sh -f /tmp/new_vars.txt
#
# WAS NICHT ZU TUN:
# =================
#   ❌ cat > .env <<EOF        # ueberschreibt — KEYS GEHEN VERLOREN
#   ❌ echo "X=y" > .env       # gleiche Falle
#   ✅ ./safe_env_update.sh "X=y"   # safe

set -e
ENV_FILE=/opt/investpilot/.env

if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE existiert nicht"
    exit 1
fi

# 1. Backup
BACKUP="${ENV_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
PRE_KEYS=$(grep -c '^[A-Z_]*=' "$ENV_FILE" || echo 0)

# 2. Process arg
if [ "$1" = "-f" ]; then
    SOURCE="$2"
    [ -f "$SOURCE" ] || { echo "FAIL: $SOURCE not found"; exit 1; }
    NEW_VARS=$(cat "$SOURCE")
else
    NEW_VARS="$@"
fi

# 3. Apply each KEY=value
echo "$NEW_VARS" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    KEY=$(echo "$line" | cut -d= -f1)
    [ -z "$KEY" ] && continue
    if grep -q "^${KEY}=" "$ENV_FILE"; then
        # Replace existing in-place
        sed -i "s|^${KEY}=.*|${line}|" "$ENV_FILE"
        echo "[REPLACED] $KEY"
    else
        # Append new
        echo "$line" >> "$ENV_FILE"
        echo "[ADDED] $KEY"
    fi
done

# 4. Verify: no key was lost
POST_KEYS=$(grep -c '^[A-Z_]*=' "$ENV_FILE" || echo 0)
if [ "$POST_KEYS" -lt "$PRE_KEYS" ]; then
    echo "FAIL: Keys verloren ($PRE_KEYS -> $POST_KEYS). Restore from $BACKUP"
    cp "$BACKUP" "$ENV_FILE"
    exit 1
fi

chmod 600 "$ENV_FILE"
echo "OK: .env updated, $PRE_KEYS -> $POST_KEYS keys, backup at $BACKUP"
