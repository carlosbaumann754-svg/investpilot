"""
Standalone Optimizer-Runner.

Zwei Betriebsarten:
1) **Subprocess auf Render** (Legacy v9): Wird von web/app.py per Popen gestartet,
   damit ein OOM-Kill durch den Optimizer NICHT den Haupt-Container toetet.
   ABER: Render Free Tier killt cgroup-weit, daher unzuverlaessig.
2) **GitHub Action (v10)**: Laeuft im CI-Runner mit 7 GB RAM, restored zuerst
   den Brain-State aus dem Gist, fuehrt die Optimierung durch und pusht NUR
   die Optimizer-Output-Dateien zurueck (keine Race-Condition mit Trading-Server).

Usage:
    python -m app.optimizer_runner [triggered_by]

    triggered_by: beliebiger Identifier ("manual", "scheduler", "github-action").
                  Wenn er mit "github-action" beginnt, wird der CI-Mode aktiv.

ENV:
    INVESTPILOT_OPTIMIZER_CI=1     erzwingt CI-Mode (Restore + isolierter Push)
    GITHUB_TOKEN                   Pflicht im CI-Mode (Gist Read/Write)
"""

import logging
import os
import sys
from datetime import datetime

# Basis-Logging fuer den Subprozess
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OPT-RUNNER] [%(levelname)s] %(message)s",
)
log = logging.getLogger("optimizer_runner")


def _write_status(status):
    try:
        from app.config_manager import save_json
        save_json("optimizer_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _is_ci_mode(triggered_by: str) -> bool:
    if os.environ.get("INVESTPILOT_OPTIMIZER_CI", "0") == "1":
        return True
    return triggered_by.startswith("github-action")


def _shard_mode_config():
    """Liest Shard-Konfiguration aus Env. Returns (shard_id, num_shards) oder None."""
    if os.environ.get("INVESTPILOT_OPTIMIZER_SHARD_MODE", "0") != "1":
        return None
    try:
        shard_id = int(os.environ["INVESTPILOT_OPTIMIZER_SHARD"])
        num_shards = int(os.environ["INVESTPILOT_OPTIMIZER_NUM_SHARDS"])
    except (KeyError, ValueError) as e:
        log.error(f"SHARD_MODE aktiv aber SHARD/NUM_SHARDS ungueltig: {e}")
        return None
    if shard_id < 0 or shard_id >= num_shards:
        log.error(f"SHARD-Index {shard_id} ausserhalb [0,{num_shards})")
        return None
    return (shard_id, num_shards)


def _is_merge_mode() -> bool:
    return os.environ.get("INVESTPILOT_OPTIMIZER_MERGE_MODE", "0") == "1"


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "subprocess"
    pid = os.getpid()
    ci_mode = _is_ci_mode(triggered_by)
    shard_cfg = _shard_mode_config()
    merge_mode = _is_merge_mode()

    if shard_cfg and merge_mode:
        log.error("SHARD_MODE und MERGE_MODE gleichzeitig — Abbruch")
        sys.exit(2)

    if shard_cfg:
        mode_label = f"github-action-shard-{shard_cfg[0]+1}/{shard_cfg[1]}"
    elif merge_mode:
        mode_label = "github-action-merge"
    elif ci_mode:
        mode_label = "github-action"
    else:
        mode_label = "subprocess"

    log.info(f"Optimizer-Runner gestartet (PID {pid}, mode={mode_label}, "
             f"triggered_by={triggered_by})")

    # CI-Mode: erst Brain-State aus Gist holen
    if ci_mode:
        try:
            from app.persistence import restore_for_optimizer
            ok = restore_for_optimizer()
            if not ok:
                log.error("CI-Mode: Restore aus Gist fehlgeschlagen — Abbruch")
                _write_status({
                    "state": "error",
                    "started_at": datetime.now().isoformat(),
                    "finished_at": datetime.now().isoformat(),
                    "triggered_by": triggered_by,
                    "error": "restore_for_optimizer fehlgeschlagen",
                    "pid": pid,
                    "mode": mode_label,
                })
                sys.exit(2)
            log.info("CI-Mode: Brain-State erfolgreich aus Gist restauriert")
        except Exception as e:
            log.exception("CI-Mode: Restore-Fehler")
            sys.exit(2)

        # Im CI-Mode den inline-Backup im optimizer.py unterdruecken,
        # damit wir am Ende isoliert nur die Output-Files pushen.
        os.environ["INVESTPILOT_SKIP_INLINE_BACKUP"] = "1"

    status = {
        "state": "running",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "triggered_by": triggered_by,
        "action": None,
        "error": None,
        "pid": pid,
        "mode": mode_label,
    }
    _write_status(status)

    try:
        if shard_cfg:
            from app.optimizer import run_shard_optimization
            result = run_shard_optimization(shard_cfg[0], shard_cfg[1])
        elif merge_mode:
            from app.optimizer import run_merge_optimization
            result = run_merge_optimization()
        else:
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
        log.exception("MemoryError im Optimizer-Runner")
        status["state"] = "error"
        status["error"] = f"MemoryError: {e}"
    except Exception as e:
        log.exception("Fehler im Optimizer-Runner")
        status["state"] = "error"
        status["error"] = f"{type(e).__name__}: {e}"

    status["finished_at"] = datetime.now().isoformat()
    _write_status(status)

    # CI-Mode: Optimizer-Output isoliert in den Gist pushen.
    # Shard-Jobs pushen NICHT (ihre Resultate landen als GH-Artifact und
    # werden vom Merge-Job konsumiert). Nur Single- und Merge-Mode pushen.
    if ci_mode and not shard_cfg:
        try:
            from app.persistence import backup_optimizer_results
            ok = backup_optimizer_results()
            if ok:
                log.info("CI-Mode: Optimizer-Output erfolgreich in Gist gepusht")
            else:
                log.error("CI-Mode: backup_optimizer_results fehlgeschlagen")
        except Exception as e:
            log.exception(f"CI-Mode: Push-Fehler: {e}")
    elif shard_cfg:
        log.info(f"Shard-Mode: kein Gist-Push (Shard-Resultate via GH Artifact)")

    if status["state"] == "error":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
