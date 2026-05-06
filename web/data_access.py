"""
InvestPilot - Data Access Layer
Thread-safe Zugriff auf JSON-Dateien und Trading-Flag.
"""

import json
import os
import threading
from pathlib import Path

from app.config_manager import get_data_path

_file_locks = {}
_lock_mutex = threading.Lock()


def _get_lock(filename):
    """Thread-Lock pro Datei."""
    with _lock_mutex:
        if filename not in _file_locks:
            _file_locks[filename] = threading.Lock()
        return _file_locks[filename]


def read_json_safe(filename):
    """JSON-Datei thread-safe lesen."""
    path = get_data_path(filename)
    if not path.exists():
        return None
    lock = _get_lock(filename)
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def write_json_safe(filename, data):
    """JSON-Datei thread-safe und atomic schreiben."""
    path = get_data_path(filename)
    lock = _get_lock(filename)
    with lock:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(str(tmp), str(path))


def get_trading_status():
    """Lese Trading-Status (enabled/disabled, letzter Lauf).

    v37cw: Fail-CLOSED Default. Wenn Flag-File fehlt oder unleserlich ist,
    behandeln wir Trading als DEAKTIVIERT (konservativ). Frueher: Default=True.
    Verhinderte Episode 05.05.2026 wo Container-Rebuild die Flag verlor und
    Bot ueber Nacht 6 unintended Limit-Orders submittete.
    """
    flag_path = get_data_path("trading_enabled.flag")
    enabled = False  # fail-closed
    try:
        if flag_path.exists():
            content = flag_path.read_text().strip().lower()
            enabled = content in ("true", "1")
    except Exception:
        enabled = False

    # Letzter Lauf aus brain_state
    brain = read_json_safe("brain_state.json")
    last_run = None
    if brain and brain.get("performance_snapshots"):
        last_snap = brain["performance_snapshots"][-1]
        last_run = f"{last_snap['date']} {last_snap['time']}"

    return {
        "enabled": enabled,
        "last_run": last_run,
        "total_runs": brain.get("total_runs", 0) if brain else 0,
    }


def set_trading_enabled(enabled: bool):
    """Trading aktivieren/deaktivieren."""
    flag_path = get_data_path("trading_enabled.flag")
    flag_path.write_text("true" if enabled else "false")


def read_log_tail(lines=100):
    """Letzte N Zeilen des Trading-Logs lesen."""
    log_path = get_data_path("logs/scheduler.log")
    if not log_path.exists():
        # Fallback: altes Log-Format
        log_path = get_data_path("logs/demo_trader.log")
    if not log_path.exists():
        return []

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return [line.rstrip() for line in all_lines[-lines:]]
    except Exception:
        return []
