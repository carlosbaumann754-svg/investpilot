"""
InvestPilot - Cloud Persistence
Sichert Brain-State, Trade-History und Scanner-State als privates GitHub Gist.
Stellt Daten beim Container-Start automatisch wieder her.
So ueberleben Learnings jeden Render-Restart/Redeploy.
"""

import json
import os
import logging
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

from app.config_manager import get_data_path, load_json, save_json

log = logging.getLogger("Persistence")

GITHUB_API = "https://api.github.com"
GIST_DESCRIPTION = "InvestPilot Brain Backup (auto-managed)"

# Dateien die gesichert werden sollen
BACKUP_FILES = [
    "brain_state.json",
    "trade_history.json",
    "scanner_state.json",
    "config.json",
    "risk_state.json",
    "execution_log.json",
    "trailing_sl_state.json",
    "decision_log.json",
    "alert_state.json",
    "market_context.json",
    "discovery_result.json",
    "weekly_report.json",
    "backtest_results.json",
    "ml_model.json",
    "optimization_history.json",
]


def _get_token():
    """GitHub Token aus Environment Variable laden."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.debug("GITHUB_TOKEN nicht gesetzt - Persistence deaktiviert")
    return token


def _headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _find_backup_gist(token):
    """Finde existierendes Backup-Gist anhand der Description."""
    try:
        resp = requests.get(
            f"{GITHUB_API}/gists",
            headers=_headers(token),
            params={"per_page": 30},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Gist-Liste laden fehlgeschlagen: HTTP {resp.status_code}")
            return None

        for gist in resp.json():
            if gist.get("description") == GIST_DESCRIPTION:
                return gist["id"]
    except Exception as e:
        log.warning(f"Gist-Suche fehlgeschlagen: {e}")
    return None


def backup_to_cloud():
    """Sichere alle Brain-Daten als GitHub Gist."""
    token = _get_token()
    if not token or not requests:
        return False

    # Dateien sammeln
    files = {}
    for filename in BACKUP_FILES:
        data = load_json(filename)
        if data is not None:
            files[filename] = {
                "content": json.dumps(data, indent=2, ensure_ascii=False, default=str)
            }

    if not files:
        log.debug("Keine Daten zum Sichern")
        return False

    # Timestamp hinzufuegen
    files["_backup_meta.json"] = {
        "content": json.dumps({
            "last_backup": datetime.now().isoformat(),
            "files": list(files.keys()),
        }, indent=2)
    }

    try:
        gist_id = _find_backup_gist(token)

        if gist_id:
            # Update existierendes Gist
            resp = requests.patch(
                f"{GITHUB_API}/gists/{gist_id}",
                headers=_headers(token),
                json={"files": files},
                timeout=20,
            )
        else:
            # Neues Gist erstellen (privat)
            resp = requests.post(
                f"{GITHUB_API}/gists",
                headers=_headers(token),
                json={
                    "description": GIST_DESCRIPTION,
                    "public": False,
                    "files": files,
                },
                timeout=20,
            )

        if resp.status_code in (200, 201):
            gist_data = resp.json()
            log.info(f"  Cloud-Backup OK ({len(files)} Dateien -> Gist {gist_data['id'][:8]}...)")
            return True
        else:
            log.warning(f"  Cloud-Backup fehlgeschlagen: HTTP {resp.status_code}")
            return False

    except Exception as e:
        log.warning(f"  Cloud-Backup Fehler: {e}")
        return False


def restore_from_cloud():
    """Stelle Brain-Daten aus GitHub Gist wieder her."""
    token = _get_token()
    if not token or not requests:
        log.info("  Cloud-Restore: Kein Token, ueberspringe")
        return False

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            log.info("  Cloud-Restore: Kein Backup-Gist gefunden (erster Start)")
            return False

        resp = requests.get(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"  Cloud-Restore fehlgeschlagen: HTTP {resp.status_code}")
            return False

        gist_data = resp.json()
        files_restored = 0

        for filename in BACKUP_FILES:
            if filename in gist_data.get("files", {}):
                content = gist_data["files"][filename].get("content", "")
                if content:
                    # Wiederherstellen wenn lokal keine/leere Daten
                    local_data = load_json(filename)
                    is_empty = (
                        local_data is None
                        or local_data == []
                        or local_data == {}
                        or (isinstance(local_data, dict) and local_data.get("total_runs", 0) == 0
                            and len(local_data) <= 1)
                    )
                    if is_empty:
                        data = json.loads(content)
                        save_json(filename, data)
                        files_restored += 1
                        log.info(f"    Wiederhergestellt: {filename}")
                    else:
                        log.debug(f"    Uebersprungen (lokal vorhanden): {filename}")

        # Meta-Info lesen
        meta = gist_data.get("files", {}).get("_backup_meta.json", {})
        if meta:
            try:
                meta_data = json.loads(meta.get("content", "{}"))
                last_backup = meta_data.get("last_backup", "unbekannt")
                log.info(f"  Cloud-Restore OK: {files_restored} Dateien wiederhergestellt "
                         f"(Backup von {last_backup})")
            except Exception:
                pass

        return files_restored > 0

    except Exception as e:
        log.warning(f"  Cloud-Restore Fehler: {e}")
        return False


def restore_from_cloud_with_gdrive():
    """Restore from GitHub Gist (Google Drive deaktiviert — SA Quota-Limit)."""
    return restore_from_cloud()
