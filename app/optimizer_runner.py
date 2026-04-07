"""
Standalone Optimizer-Runner — wird als separater Python-Prozess gestartet,
damit ein OOM-Kill durch den Optimizer NICHT den Haupt-Container toetet.

Usage:
    python -m app.optimizer_runner [triggered_by]

Schreibt den Fortschritt nach data/optimizer_status.json, sodass das
Dashboard / der Watchdog den Zustand beobachten kann.
"""

import logging
import os
import sys
from datetime import datetime

# Basis-Logging fuer den Subprozess
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OPT-SUBPROC] [%(levelname)s] %(message)s",
)
log = logging.getLogger("optimizer_runner")


def _write_status(status):
    try:
        from app.config_manager import save_json
        save_json("optimizer_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "subprocess"
    pid = os.getpid()

    log.info(f"Optimizer-Subprozess gestartet (PID {pid}, triggered_by={triggered_by})")

    status = {
        "state": "running",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": triggered_by,
        "action": None,
        "error": None,
        "pid": pid,
        "mode": "subprocess",
    }
    _write_status(status)

    try:
        from app.optimizer import run_weekly_optimization
        result = run_weekly_optimization()
        if isinstance(result, dict):
            status["action"] = result.get("action", "unknown")
            if result.get("action") == "error":
                status["state"] = "error"
                status["error"] = result.get("error", "unknown")
            else:
                status["state"] = "done"
        else:
            status["state"] = "done"
            status["action"] = "unknown"
        log.info(f"Optimizer abgeschlossen: action={status['action']}")
    except MemoryError as e:
        log.exception("MemoryError im Optimizer-Subprozess")
        status["state"] = "error"
        status["error"] = f"MemoryError: {e}"
    except Exception as e:
        log.exception("Fehler im Optimizer-Subprozess")
        status["state"] = "error"
        status["error"] = f"{type(e).__name__}: {e}"

    status["finished_at"] = datetime.now().isoformat()
    _write_status(status)

    if status["state"] == "error":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
