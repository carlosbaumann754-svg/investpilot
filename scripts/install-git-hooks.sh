#!/usr/bin/env bash
# investpilot — Git-Hooks-Installer
#
# Was: kopiert versionierte Hooks aus scripts/git-hooks/ nach .git/hooks/
# Warum: .git/hooks/ ist NICHT in git versioniert — nach `git clone` muessen
#        Hooks manuell wiederhergestellt werden. Dieses Skript automatisiert das.
#
# Verwendung:
#   bash scripts/install-git-hooks.sh
#
# Installiert:
#   - post-commit (Roadmap-Update-Reminder)
#   - pre-commit  (Audit-Coverage-Marker-Check, v37h+3 19.05.2026)

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_DIR="$REPO_ROOT/scripts/git-hooks"
DST_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$SRC_DIR" ]; then
    echo "FEHLER: $SRC_DIR existiert nicht."
    exit 1
fi

if [ ! -d "$DST_DIR" ]; then
    echo "FEHLER: $DST_DIR existiert nicht (kein git-Repo?)."
    exit 1
fi

INSTALLED=0
for hook in "$SRC_DIR"/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    cp "$hook" "$DST_DIR/$name"
    chmod +x "$DST_DIR/$name"
    echo "  installiert: $name"
    INSTALLED=$((INSTALLED + 1))
done

echo ""
echo "OK — $INSTALLED Hook(s) nach $DST_DIR kopiert."
echo ""
echo "Verifikation:"
echo "  ls -la .git/hooks/ | grep -v sample"
