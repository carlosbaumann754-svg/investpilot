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
    "auth_2fa.json",
    "meta_model.json",
    "meta_labeling_shadow.json",
    "partial_close_state.json",
]

# Dateien die zwar gesichert werden, aber nie aus der Cloud RESTORED werden duerfen.
#
# v9-Historie: Damals lief der Optimizer als Subprocess IM Render-Container.
# Bei OOM-Kill blieb optimizer_status.json mit toter PID auf state=running
# stehen und blockierte den Optimizer-Slot. Fix damals: nicht restoren, der
# Stale-Lock-Recovery in /api/optimizer/run raeumt auf.
#
# v10-Aenderung: Der Optimizer laeuft jetzt als GitHub Action — der Gist-Stand
# (von der GH Action geschrieben) IST die autoritative Quelle. Wenn der Render
# beim Restart NICHT restored, bleibt seine LOKALE Datei fuer immer auf
# "running, dispatching" haengen → Watchdog meldet permanent stale lock.
# Loesung: Set ist jetzt leer, Gist-Stand wird beim Restart restauriert.
NO_RESTORE_FILES: set[str] = set()

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


# ============================================================
# NAMED SNAPSHOTS — Point-in-Time Restore Points
# ============================================================
# Im Gegensatz zu backup_to_cloud() (Rolling Backup, wird staendig
# ueberschrieben) erzeugt create_named_snapshot() eine unveraenderliche
# Kopie aller Backup-Dateien unter einem benannten Dateinamen. Diese
# ueberlebt spaetere backup_to_cloud()-Aufrufe, weil der Dateiname ausserhalb
# der BACKUP_FILES-Liste liegt und PATCH-Requests andere Gist-Files nie
# anfassen. Empfohlen vor groesseren Upgrades, Migrationen oder Experimenten.

SNAPSHOT_FILE_PREFIX = "snapshot_"


def create_named_snapshot(name: str, note: str = "") -> dict:
    """Erzeugt einen benannten Point-in-Time-Snapshot im Backup-Gist.

    Args:
        name: Menschenlesbarer Name (wird ge-sanitized fuer Dateinamen).
        note: Optionale Beschreibung / Kontext.

    Returns:
        dict mit success/filename/file_count oder error.
    """
    import re
    token = _get_token()
    if not token or not requests:
        return {"error": "GITHUB_TOKEN nicht gesetzt"}

    # Alle aktuellen Backup-Dateien in ein Bundle packen
    bundle = {}
    for filename in BACKUP_FILES:
        data = load_json(filename)
        if data is not None:
            bundle[filename] = data

    if not bundle:
        return {"error": "Keine Daten zum Snapshotten gefunden"}

    # Sicherer Dateiname: nur [a-zA-Z0-9_-]
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())[:60] or "unnamed"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_filename = f"{SNAPSHOT_FILE_PREFIX}{safe_name}_{timestamp}.json"

    snapshot_content = {
        "name": name,
        "note": note,
        "created_at": datetime.now().isoformat(),
        "file_count": len(bundle),
        "files": bundle,
    }

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            return {"error": "Kein Backup-Gist gefunden (erst backup_to_cloud() ausfuehren)"}

        payload_content = json.dumps(
            snapshot_content, indent=2, ensure_ascii=False, default=str
        )
        size_kb = len(payload_content.encode("utf-8")) / 1024

        resp = requests.patch(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            json={"files": {snapshot_filename: {"content": payload_content}}},
            timeout=60,
        )

        if resp.status_code in (200, 201):
            log.info(f"Named-Snapshot erstellt: {snapshot_filename} "
                     f"({len(bundle)} Dateien, {size_kb:.1f} KB)")
            return {
                "success": True,
                "filename": snapshot_filename,
                "name": name,
                "file_count": len(bundle),
                "size_kb": round(size_kb, 1),
                "created_at": snapshot_content["created_at"],
            }
        return {"error": f"Gist PATCH fehlgeschlagen: HTTP {resp.status_code}"}
    except Exception as e:
        log.error(f"create_named_snapshot Fehler: {e}")
        return {"error": str(e)}


def list_named_snapshots() -> list:
    """Listet alle Named-Snapshots im Backup-Gist (neueste zuerst)."""
    token = _get_token()
    if not token or not requests:
        return []

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            return []
        resp = requests.get(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        files = resp.json().get("files", {})
        snapshots = []
        for filename, meta in files.items():
            if filename.startswith(SNAPSHOT_FILE_PREFIX):
                snapshots.append({
                    "filename": filename,
                    "size_bytes": meta.get("size", 0),
                })
        return sorted(snapshots, key=lambda x: x["filename"], reverse=True)
    except Exception as e:
        log.warning(f"list_named_snapshots Fehler: {e}")
        return []


def restore_named_snapshot(filename: str) -> dict:
    """Stellt einen benannten Snapshot wieder her.

    ACHTUNG: Ueberschreibt aktuelle lokale Dateien mit dem Stand im Snapshot.
    Nicht automatisch gebackupt — der Caller sollte vorher einen neuen
    Snapshot des aktuellen Stands ziehen.
    """
    if not filename.startswith(SNAPSHOT_FILE_PREFIX):
        return {"error": "Ungueltiger Snapshot-Dateiname"}

    token = _get_token()
    if not token or not requests:
        return {"error": "GITHUB_TOKEN nicht gesetzt"}

    try:
        gist_id = _find_backup_gist(token)
        if not gist_id:
            return {"error": "Kein Backup-Gist gefunden"}

        resp = requests.get(
            f"{GITHUB_API}/gists/{gist_id}",
            headers=_headers(token),
            timeout=30,
        )
        if resp.status_code != 200:
            return {"error": f"Gist-Fetch fehlgeschlagen: HTTP {resp.status_code}"}

        files = resp.json().get("files", {})
        entry = files.get(filename)
        if not entry:
            return {"error": f"Snapshot '{filename}' nicht gefunden"}

        content = _fetch_gist_file_content(entry, token)
        if not content:
            return {"error": "Snapshot-Inhalt leer"}

        parsed = json.loads(content)
        bundle = parsed.get("files", {})
        if not bundle:
            return {"error": "Snapshot enthaelt keine Dateien"}

        restored = 0
        for fname, fdata in bundle.items():
            save_json(fname, fdata)
            restored += 1

        log.info(f"restore_named_snapshot: {restored} Dateien aus {filename} wiederhergestellt")
        return {
            "success": True,
            "restored_files": restored,
            "snapshot_name": parsed.get("name"),
            "created_at": parsed.get("created_at"),
        }
    except Exception as e:
        log.error(f"restore_named_snapshot Fehler: {e}")
        return {"error": str(e)}
