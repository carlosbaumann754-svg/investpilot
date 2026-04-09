"""
Standalone ML-Training-Runner — laeuft auf GitHub Actions (7 GB RAM).

Mirror zu backtest_runner / optimizer_runner. Grund: Render Free Tier (512 MB)
kann `download_history(years=5)` fuer 70+ Symbole + RandomForest-Training nicht
ausfuehren, ohne den Web-Container zu OOMen. Ein GH Actions Runner hat 7 GB RAM.

Workflow:
  1. Restore Brain-State + Config aus Gist (fuer Trade-History, config.json)
  2. download_history(years=5) + train_model()
  3. ml_model.json + ml_training_status.json + ml_model_weights.json (joblib
     base64-encoded) isoliert in den Gist pushen (via backup_ml_training_results)
  4. Render-Watchdog (check_and_reload_ml_training_output) laedt die Files +
     dekodiert das joblib-Binary zurueck auf Disk

Usage:
    python -m app.ml_training_runner [triggered_by]

ENV:
    GITHUB_TOKEN   Pflicht (Gist Read/Write)
"""

import logging
import sys
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ML-RUNNER] [%(levelname)s] %(message)s",
)
log = logging.getLogger("ml_training_runner")


def _write_status(**fields):
    try:
        from app.config_manager import load_json, save_json
        status = load_json("ml_training_status.json") or {}
        status.update(fields)
        status["updated_at"] = datetime.now().isoformat()
        save_json("ml_training_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _push_results():
    try:
        from app.persistence import backup_ml_training_results
        ok = backup_ml_training_results()
        log.info(f"Push to Gist: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        log.warning(f"Push fehlgeschlagen: {e}")
        return False


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "manual"
    started_at = datetime.now().isoformat()

    log.info("=" * 55)
    log.info(f"ML-TRAINING-RUNNER START (triggered_by={triggered_by})")
    log.info("=" * 55)

    _write_status(
        state="running",
        phase="init",
        message="Runner gestartet",
        started_at=started_at,
        finished_at=None,
        triggered_by=triggered_by,
        error=None,
        mode="github-action-running",
    )
    _push_results()

    # 1) Restore from Gist so we have current trade_history + config
    try:
        from app.persistence import restore_from_cloud
        restore_from_cloud()
        log.info("Cloud-Restore OK")
    except Exception as e:
        log.warning(f"Cloud-Restore fehlgeschlagen (weiter mit lokalem Stand): {e}")

    # 2) Download history + train
    try:
        _write_status(
            state="running", phase="download",
            message="Lade 5 Jahre Historie fuer alle Symbole...",
        )
        _push_results()

        from app.backtester import download_history
        from app.ml_scorer import train_model

        histories = download_history(years=5)
        if not histories:
            raise RuntimeError("Keine historischen Daten (download_history leer)")

        _write_status(
            state="running", phase="train",
            message=f"Training auf {len(histories)} Symbolen...",
        )
        _push_results()

        result = train_model(histories)

        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"])

        summary = (
            f"trades_used={result.get('trades_used', 0)}, "
            f"test_acc={result.get('test_accuracy', 0)}%, "
            f"test_f1={result.get('test_f1', 0)}%, "
            f"threshold={result.get('tuned_threshold', 0.5)}"
        )
        log.info(f"ML-Training OK: {summary}")

        _write_status(
            state="done",
            phase="done",
            message="ML-Modell trainiert",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=None,
            mode="github-action-done",
            model_info=result,
            summary=summary,
        )
        _push_results()
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"ML-Training fehlgeschlagen: {e}\n{tb}")
        _write_status(
            state="error",
            phase="error",
            message=f"{type(e).__name__}: {e}",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=f"{type(e).__name__}: {e}",
            mode="github-action-error",
            traceback=tb[-2000:],
        )
        _push_results()
        return 1


if __name__ == "__main__":
    sys.exit(main())
