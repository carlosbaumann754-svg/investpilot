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
    "optimizer_status.json",
]

# Dateien die zwar gesichert werden, aber nie aus der Cloud RESTORED werden duerfen.
# Grund: Optimizer-Status ist ephemer. Beim Container-Restart wuerde sonst ein
# alter "running" Status mit toter PID zurueckkommen und den Optimizer-Slot
# blockieren (siehe v9 Stale-Lock-Bug).
NO_RESTORE_FILES = {
    "optimizer_status.json",
}

# Dateien die der Optimizer modifiziert. Werden vom GitHub-Action-Optimizer-Push
# isoliert in den Gist geschrieben, um Race-Conditions mit Trading-Server-Updates
# (brain_state, trade_history) zu vermeiden.
OPTIMIZER_OUTPUT_FILES = [
    "config.json",
    "optimization_history.json",
    "optimizer_status.json",
    "ml_model.json",
    "backtest_results.json",
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


def _fetch_gist_file_content(file_entry, token):
    """
    Hole den vollstaendigen Inhalt einer Gist-Datei.
    Wenn die GitHub-API die Datei als truncated markiert hat
    (Groesse > ~1 MB), wird der Inhalt ueber raw_url nachgeladen.
    Gibt den rohen Text-Content oder None bei Fehler zurueck.
    """
    content = file_entry.get("content", "")
    truncated = file_entry.get("truncated", False)
    if not truncated and content:
        return content

    raw_url = file_entry.get("raw_url")
    if not raw_url:
        return content or None

    try:
        # raw_url zeigt auf gist.githubusercontent.com
        # Privat-Gist: Token als Authorization-Header senden
        headers = {"Authorization": f"token {token}"} if token else {}
        r = requests.get(raw_url, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.text
        log.warning(f"raw_url fetch fehlgeschlagen: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"raw_url fetch Fehler: {e}")
    return content or None


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
            if filename in NO_RESTORE_FILES:
                log.debug(f"    Skip restore (NO_RESTORE_FILES): {filename}")
                continue
            if filename in gist_data.get("files", {}):
                file_entry = gist_data["files"][filename]
                content = _fetch_gist_file_content(file_entry, token)
                if not content:
                    continue
                try:
                    gist_parsed = json.loads(content)
                except Exception as e:
                    log.warning(f"    Parse-Fehler bei Gist-{filename}: {e}")
                    continue

                local_data = load_json(filename)

                # Entscheidungslogik fuer Restore:
                # 1. Lokale Datei fehlt/leer  -> immer restore
                # 2. brain_state.json: restore wenn Gist mehr total_runs hat
                #    (schuetzt gegen OOM-Reset wo Scheduler 1 Zyklus schreibt
                #     bevor Restore laeuft)
                # 3. trade_history.json: restore wenn Gist mehr Trades hat
                # 4. Sonstige: restore wenn lokal None/leer
                should_restore = False
                reason = ""

                if local_data is None or local_data == [] or local_data == {}:
                    should_restore = True
                    reason = "lokal leer"
                elif filename == "brain_state.json" and isinstance(gist_parsed, dict):
                    local_runs = (local_data.get("total_runs", 0)
                                  if isinstance(local_data, dict) else 0)
                    gist_runs = gist_parsed.get("total_runs", 0)
                    if gist_runs > local_runs:
                        should_restore = True
                        reason = f"gist runs={gist_runs} > local runs={local_runs}"
                elif filename == "trade_history.json" and isinstance(gist_parsed, list):
                    local_count = len(local_data) if isinstance(local_data, list) else 0
                    if len(gist_parsed) > local_count:
                        should_restore = True
                        reason = f"gist trades={len(gist_parsed)} > local={local_count}"
                elif isinstance(local_data, dict) and local_data.get("total_runs", 0) == 0 \
                        and len(local_data) <= 1:
                    should_restore = True
                    reason = "lokal Dummy-State"

                if should_restore:
                    save_json(filename, gist_parsed)
                    files_restored += 1
                    log.info(f"    Wiederhergestellt: {filename} ({reason})")
                else:
                    log.debug(f"    Uebersprungen (lokal aktueller/gleich): {filename}")

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


def restore_for_optimizer():
    """
    Restore-Variante fuer den GitHub-Action-Optimizer.

    Holt ALLE Backup-Dateien (auch brain_state.json, trade_history.json),
    weil der CI-Runner mit leerem data/-Verzeichnis startet und der Optimizer
    den aktuellen Brain-State + Trade-Historie braucht, um sinnvolle Backtests
    zu rechnen. Im Gegensatz zu restore_from_cloud() ueberspringt diese
    Variante NICHTS und nutzt keine "should_restore"-Heuristik — der CI-Runner
    hat per Definition keine lokalen Daten zu schuetzen.
    """
    token = _get_token()
    if not token or not requests:
        log.warning("restore_for_optimizer: Kein GITHUB_TOKEN — Abbruch")
        return False

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            log.warning("restore_for_optimizer: Kein Backup-Gist gefunden")
            return False

        resp = requests.get(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"restore_for_optimizer: HTTP {resp.status_code}")
            return False

        gist_data = resp.json()
        files_restored = 0
        for filename in BACKUP_FILES:
            if filename in NO_RESTORE_FILES:
                continue
            if filename not in gist_data.get("files", {}):
                continue
            file_entry = gist_data["files"][filename]
            content = _fetch_gist_file_content(file_entry, token)
            if not content:
                continue
            try:
                parsed = json.loads(content)
            except Exception as e:
                log.warning(f"restore_for_optimizer: parse {filename}: {e}")
                continue
            save_json(filename, parsed)
            files_restored += 1
        log.info(f"restore_for_optimizer: {files_restored} Dateien geladen")
        return files_restored > 0
    except Exception as e:
        log.warning(f"restore_for_optimizer Fehler: {e}")
        return False


def backup_optimizer_results():
    """
    Push NUR die Dateien, die der Optimizer modifiziert.

    Wird vom GitHub-Action-Runner am Ende eines Optimizer-Laufs aufgerufen.
    Vermeidet die Race-Condition mit dem Trading-Server: Wuerden wir
    backup_to_cloud() nutzen, wuerden wir brain_state.json / trade_history.json
    aus dem Stand zu Optimizer-Start ueberschreiben — und damit alle Trades
    der letzten ~20 Minuten verlieren.
    """
    token = _get_token()
    if not token or not requests:
        log.warning("backup_optimizer_results: Kein GITHUB_TOKEN")
        return False

    files = {}
    for filename in OPTIMIZER_OUTPUT_FILES:
        data = load_json(filename)
        if data is not None:
            files[filename] = {
                "content": json.dumps(data, indent=2, ensure_ascii=False, default=str)
            }

    if not files:
        log.warning("backup_optimizer_results: Keine Optimizer-Output-Dateien gefunden")
        return False

    files["_optimizer_meta.json"] = {
        "content": json.dumps({
            "last_optimizer_push": datetime.now().isoformat(),
            "files": list(files.keys()),
            "source": "github-action",
        }, indent=2)
    }

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            log.warning("backup_optimizer_results: Kein Backup-Gist gefunden")
            return False

        # PATCH: aktualisiert nur die uebergebenen Files, laesst andere unberuehrt.
        resp = requests.patch(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            json={"files": files},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            log.info(f"backup_optimizer_results OK ({len(files)} Dateien)")
            return True
        log.warning(f"backup_optimizer_results: HTTP {resp.status_code}")
        return False
    except Exception as e:
        log.warning(f"backup_optimizer_results Fehler: {e}")
        return False
