"""
InvestPilot - Google Drive Backup
Sichert Brain-State, Trade-History, Optimizer-Results, Weekly Reports und Logs
auf Google Drive via Service Account.
Graceful Degradation: Wenn keine Google-Credentials gesetzt sind, wird
alles uebersprungen ohne Fehler.
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("GDriveBackup")

# Files to backup (same list as persistence.py for consistency)
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

# In-memory cache of file hashes to avoid re-uploading unchanged files
_upload_cache = {}


def _get_credentials():
    """Load Google Service Account credentials from env var.

    GDRIVE_SERVICE_ACCOUNT_JSON can be either:
    - A JSON string with the service account key
    - A file path to the JSON key file

    Returns credentials object or None if not configured.
    """
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None

    try:
        from google.oauth2 import service_account

        scopes = ["https://www.googleapis.com/auth/drive.file"]

        # Try as file path first
        if os.path.isfile(sa_json):
            creds = service_account.Credentials.from_service_account_file(
                sa_json, scopes=scopes
            )
        else:
            # Treat as JSON string
            import json as _json
            info = _json.loads(sa_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes
            )
        return creds
    except ImportError:
        log.debug("google-auth nicht installiert - GDrive-Backup deaktiviert")
        return None
    except Exception as e:
        log.warning(f"GDrive Credentials fehlerhaft: {e}")
        return None


def _get_folder_id():
    """Get the target Google Drive folder ID from env."""
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "")
    if not folder_id:
        log.debug("GDRIVE_FOLDER_ID nicht gesetzt - GDrive-Backup deaktiviert")
    return folder_id


def _build_service(creds):
    """Build the Google Drive API service."""
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _file_hash(filepath):
    """Compute SHA256 hash of a file for change detection."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return None


def _find_existing_file(service, folder_id, filename):
    """Find a file by name in the target folder. Returns file ID or None."""
    query = (
        f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    )
    try:
        results = service.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except Exception as e:
        log.debug(f"GDrive Dateisuche fehlgeschlagen fuer {filename}: {e}")
        return None


def _get_owner_email():
    """Get the Google account email to transfer file ownership to.

    Without ownership transfer, Service Accounts hit storageQuotaExceeded
    because they have 0 GB storage. Transferring ownership to a real
    Google account uses that account's storage quota instead.
    """
    return os.environ.get("GDRIVE_OWNER_EMAIL", "")


def _transfer_ownership(service, file_id, owner_email):
    """Transfer file ownership to a real Google account.

    This is required because Service Accounts have no storage quota.
    Files owned by the SA count against its 0 GB limit.
    """
    if not owner_email:
        return
    try:
        service.permissions().create(
            fileId=file_id,
            transferOwnership=True,
            body={
                "type": "user",
                "role": "owner",
                "emailAddress": owner_email,
            },
        ).execute()
    except Exception as e:
        log.debug(f"  Ownership-Transfer fehlgeschlagen: {e}")


def _upload_file(service, folder_id, filepath, filename):
    """Upload or update a file on Google Drive.

    Returns True if uploaded, False if skipped (unchanged) or failed.
    """
    from googleapiclient.http import MediaFileUpload

    # Check hash cache - skip if file unchanged
    current_hash = _file_hash(filepath)
    if current_hash and _upload_cache.get(filename) == current_hash:
        log.debug(f"  GDrive: {filename} unveraendert, uebersprungen")
        return False

    media = MediaFileUpload(str(filepath), resumable=True)

    existing_id = _find_existing_file(service, folder_id, filename)
    owner_email = _get_owner_email()

    try:
        if existing_id:
            # Update existing file
            service.files().update(
                fileId=existing_id,
                media_body=media,
            ).execute()
        else:
            # Create new file
            file_metadata = {
                "name": filename,
                "parents": [folder_id],
            }
            created = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id",
            ).execute()

            # Transfer ownership so file counts against user's quota
            # (Service Accounts have 0 GB storage)
            if owner_email:
                _transfer_ownership(service, created["id"], owner_email)

        # Update cache on success
        if current_hash:
            _upload_cache[filename] = current_hash
        return True

    except Exception as e:
        log.warning(f"  GDrive Upload fehlgeschlagen fuer {filename}: {e}")
        return False


def _download_file(service, file_id, dest_path):
    """Download a file from Google Drive to local path."""
    from googleapiclient.http import MediaIoBaseDownload
    import io

    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(fh.getvalue())
        return True
    except Exception as e:
        log.warning(f"  GDrive Download fehlgeschlagen fuer {dest_path.name}: {e}")
        return False


def backup_to_gdrive():
    """Backup key files to Google Drive.

    Silently skips if credentials or folder ID not configured.
    Uses file hashing to avoid re-uploading unchanged files.
    """
    creds = _get_credentials()
    folder_id = _get_folder_id()
    if not creds or not folder_id:
        return False

    try:
        from app.config_manager import get_data_path
        service = _build_service(creds)
    except ImportError:
        log.debug("google-api-python-client nicht installiert - GDrive-Backup deaktiviert")
        return False
    except Exception as e:
        log.warning(f"GDrive Service-Erstellung fehlgeschlagen: {e}")
        return False

    uploaded = 0

    # Backup JSON data files
    for filename in BACKUP_FILES:
        filepath = get_data_path(filename)
        if filepath.exists() and filepath.stat().st_size > 0:
            if _upload_file(service, folder_id, filepath, filename):
                uploaded += 1

    # Backup weekly report PDFs from Bericht/ folder
    bericht_dir = Path(__file__).parent.parent / "Bericht"
    if bericht_dir.exists():
        for pdf_file in sorted(bericht_dir.glob("*.pdf")):
            pdf_name = f"Bericht/{pdf_file.name}"
            if _upload_file(service, folder_id, pdf_file, pdf_name):
                uploaded += 1

    # Upload backup meta
    try:
        meta = {
            "last_backup": datetime.now().isoformat(),
            "files_uploaded": uploaded,
            "source": "InvestPilot GDrive Backup",
        }
        meta_path = get_data_path("_gdrive_backup_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        _upload_file(service, folder_id, meta_path, "_gdrive_backup_meta.json")
    except Exception:
        pass  # Meta upload is best-effort

    if uploaded > 0:
        log.info(f"  GDrive-Backup OK: {uploaded} Dateien hochgeladen")
    else:
        log.debug("  GDrive-Backup: Keine Aenderungen")

    return uploaded > 0


def restore_from_gdrive():
    """Restore files from Google Drive if local files are empty/missing.

    Silently skips if credentials or folder ID not configured.
    Only restores files that are missing or empty locally.
    """
    creds = _get_credentials()
    folder_id = _get_folder_id()
    if not creds or not folder_id:
        return False

    try:
        from app.config_manager import get_data_path, load_json
        service = _build_service(creds)
    except ImportError:
        log.debug("google-api-python-client nicht installiert - GDrive-Restore deaktiviert")
        return False
    except Exception as e:
        log.warning(f"GDrive Service-Erstellung fehlgeschlagen: {e}")
        return False

    restored = 0

    for filename in BACKUP_FILES:
        local_path = get_data_path(filename)

        # Only restore if local is missing or empty
        local_data = load_json(filename)
        is_empty = (
            local_data is None
            or local_data == []
            or local_data == {}
            or (isinstance(local_data, dict) and local_data.get("total_runs", 0) == 0
                and len(local_data) <= 1)
        )

        if not is_empty:
            log.debug(f"    GDrive: {filename} lokal vorhanden, uebersprungen")
            continue

        file_id = _find_existing_file(service, folder_id, filename)
        if file_id:
            if _download_file(service, file_id, local_path):
                restored += 1
                log.info(f"    GDrive wiederhergestellt: {filename}")

    if restored > 0:
        log.info(f"  GDrive-Restore OK: {restored} Dateien wiederhergestellt")
    else:
        log.debug("  GDrive-Restore: Keine Wiederherstellung noetig")

    return restored > 0
