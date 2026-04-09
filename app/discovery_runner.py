"""
Standalone Asset-Discovery-Runner — laeuft auf GitHub Actions (7 GB RAM).

Mirror zu backtest_runner / ml_training_runner. Grund: Discovery macht viele
yfinance-API-Calls und kann bei Rate-Limit-Retries den Render Free Tier
kurzzeitig blockieren. Offloaden entkoppelt es vom Trading-Server und
vermeidet jede Interferenz.

Workflow:
  1. Restore Brain-State + Config aus Gist (fuer aktuelles ASSET_UNIVERSE-Snapshot,
     damit bereits vorhandene Symbole nicht doppelt discovered werden)
  2. run_weekly_discovery() ausfuehren
  3. discovery_result.json + discovered_assets.json + discovery_status.json
     isoliert in den Gist pushen (via backup_discovery_results)
  4. Render-Watchdog (check_and_reload_discovery_output) laedt die Files und
     appliziert discovered Symbole in den Live-ASSET_UNIVERSE

Usage:
    python -m app.discovery_runner [triggered_by]

ENV:
    GITHUB_TOKEN   Pflicht (Gist Read/Write)
"""

import logging
import sys
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DISC-RUNNER] [%(levelname)s] %(message)s",
)
log = logging.getLogger("discovery_runner")


def _write_status(**fields):
    try:
        from app.config_manager import load_json, save_json
        status = load_json("discovery_status.json") or {}
        status.update(fields)
        status["updated_at"] = datetime.now().isoformat()
        save_json("discovery_status.json", status)
    except Exception as e:
        log.warning(f"Status-Write fehlgeschlagen: {e}")


def _push_results():
    try:
        from app.persistence import backup_discovery_results
        ok = backup_discovery_results()
        log.info(f"Push to Gist: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        log.warning(f"Push fehlgeschlagen: {e}")
        return False


def main():
    triggered_by = sys.argv[1] if len(sys.argv) > 1 else "manual"
    started_at = datetime.now().isoformat()

    log.info("=" * 55)
    log.info(f"DISCOVERY-RUNNER START (triggered_by={triggered_by})")
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

    # Restore from Gist
    try:
        from app.persistence import restore_from_cloud
        restore_from_cloud()
        log.info("Cloud-Restore OK")
    except Exception as e:
        log.warning(f"Cloud-Restore fehlgeschlagen: {e}")

    try:
        _write_status(
            state="running", phase="scanning",
            message="eToro durchsuchen + Assets bewerten...",
        )
        _push_results()

        from app.asset_discovery import run_weekly_discovery
        result = run_weekly_discovery()

        summary = (
            f"new_found={result.get('new_found', 0)}, "
            f"evaluated={result.get('evaluated', 0)}, "
            f"added={result.get('added', 0)}"
        )
        log.info(f"Discovery OK: {summary}")

        _write_status(
            state="done",
            phase="done",
            message="Asset Discovery abgeschlossen",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            triggered_by=triggered_by,
            error=None,
            mode="github-action-done",
            result=result,
            summary=summary,
        )
        _push_results()
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Discovery fehlgeschlagen: {e}\n{tb}")
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
