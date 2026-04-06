"""
InvestPilot - Config Manager
Laedt Strategie-Config aus config.json und Secrets aus Umgebungsvariablen (.env).
API Keys werden NIE auf Disk geschrieben.
"""

import json
import os
import logging
import threading
from pathlib import Path

log = logging.getLogger("ConfigManager")

# Data-Verzeichnis: im Docker /app/data, lokal relativ zum Projekt
DATA_DIR = Path(os.environ.get("INVESTPILOT_DATA_DIR", Path(__file__).parent.parent / "data"))

# Thread-Lock fuer JSON-Dateizugriffe (verhindert Race Conditions
# zwischen Scheduler-Thread und Web-API)
_file_locks = {}
_file_locks_lock = threading.Lock()


def _get_file_lock(filename):
    """Hole oder erstelle einen Lock fuer eine bestimmte Datei."""
    with _file_locks_lock:
        if filename not in _file_locks:
            _file_locks[filename] = threading.Lock()
        return _file_locks[filename]


def get_data_path(filename):
    """Pfad zu einer Datei im Data-Verzeichnis."""
    return DATA_DIR / filename


def load_config():
    """Lade Strategie-Config aus config.json und merge mit Secrets aus .env."""
    config_path = get_data_path("config.json")
    if not config_path.exists():
        log.error(f"config.json nicht gefunden: {config_path}")
        raise FileNotFoundError(f"config.json nicht gefunden: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Secrets aus Umgebungsvariablen laden (ueberschreiben config.json)
    etoro = config.setdefault("etoro", {})
    env_mappings = {
        "ETORO_PUBLIC_KEY": "public_key",
        "ETORO_PRIVATE_KEY": "private_key",
        "ETORO_DEMO_PRIVATE_KEY": "demo_private_key",
        "ETORO_USERNAME": "username",
        "ETORO_ENVIRONMENT": "environment",
    }
    for env_var, config_key in env_mappings.items():
        val = os.environ.get(env_var)
        if val:
            etoro[config_key] = val

    # Validierung: mindestens public_key und ein private_key muessen da sein
    if not etoro.get("public_key"):
        log.warning("ETORO_PUBLIC_KEY nicht gesetzt (weder in .env noch config.json)")
    env = etoro.get("environment", "demo")
    key_name = "demo_private_key" if env == "demo" else "private_key"
    if not etoro.get(key_name):
        log.warning(f"eToro {key_name} nicht gesetzt fuer environment={env}")

    return config


def save_config(config):
    """Speichere Config OHNE Secrets. Brain-Optimierung schreibt nur Strategie-Params."""
    config_path = get_data_path("config.json")

    # Kopie erstellen, Secrets entfernen
    safe_config = json.loads(json.dumps(config))
    etoro = safe_config.get("etoro", {})
    for secret_key in ["public_key", "private_key", "demo_private_key"]:
        if secret_key in etoro:
            del etoro[secret_key]

    # Atomic write: erst temp-file, dann umbenennen
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(safe_config, f, indent=2, ensure_ascii=False)
    os.replace(str(tmp_path), str(config_path))


def load_json(filename):
    """Lade eine JSON-Datei aus dem Data-Verzeichnis (thread-safe)."""
    lock = _get_file_lock(filename)
    with lock:
        path = get_data_path(filename)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def save_json(filename, data):
    """Speichere eine JSON-Datei ins Data-Verzeichnis (atomic write, thread-safe)."""
    lock = _get_file_lock(filename)
    with lock:
        path = get_data_path(filename)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(str(tmp_path), str(path))
